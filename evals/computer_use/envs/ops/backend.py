"""Server side of Quaymark Ops, an invented operations app: a support-queue
dashboard whose numbers follow a server timeline, purchase orders beside a team
chat, a one-record expense form, and a product's reviews.

The env ports time, change and long-horizon patterns from recent computer-use
benchmarks (see README.md). Every pattern that can be switched is a quirk in
`episode.config["quirks"]`, on by default; with a quirk off the page uses the
plain version. Rewards read only this module's state and events, never the
page's own account of what it did.

Config keys, all optional:
  quirks           {name: bool}
  seed             seeds every generated value (default 11)
  timeline         the queue's clock, see TIMELINE (a test shortens it)
  stay_s           the expense form's "Stay signed in?" countdown (default 60)
  message_delay_s  seconds from the second saved PO to the team lead's message
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
import random
import re
import time
import weakref
from pathlib import Path
from typing import Any

from ...server import Episode, Response

SITE = Path(__file__).resolve().parent / "site"

QUIRKS = {
    # Queue dashboard (SentinelBench)
    "manual_refresh": "the dashboard's numbers change only when Refresh is clicked",
    "near_miss":
        "the support backlog climbs to within two tickets of the +10% line before it crosses "
        "(on the quiet task it does so twice and never crosses)",
    # Purchase orders (OSWorld 2.0, InterruptBench)
    "late_message":
        "the team lead's chat message that changes the rules arrives only after the second PO "
        "is saved, so a saved PO must be amended",
    # Expense form (OS-Marathon, MERJ)
    "sticky_fields": "Save & add another keeps the last currency and category",
    "slow_save":
        "a save answers after 2-4 s while its button stays enabled and shows nothing, so a "
        "second click saves a duplicate",
    "timeout_dialog":
        "after the fifth saved expense a 'Stay signed in? (60 s)' dialog counts down beside the "
        "form; at zero the form's unsaved values are discarded",
    # Reviews (WebChoreArena, MERJ)
    "virtualized":
        "the review list keeps only about 25 rows in the DOM, the ones near the scroll position",
}
DEFAULT_QUIRKS = {k: True for k in QUIRKS}

USER = "Ada Lovelace"
SHIFT_START_S = 9 * 3600  # the app's clock reads 09:00:00 when an episode starts

# Live episodes by id. The privileged oracle reads an episode's timeline through
# this; the page never learns when the backlog will cross.
EPISODES: "weakref.WeakValueDictionary[str, Episode]" = weakref.WeakValueDictionary()

# ─── queue dashboard ──────────────────────────────────────────────────────
TIMELINE = {
    "cross_s": [20, 40],  # the support backlog crosses +10% at a seeded time in this range
    "window_s": 60,  # a page up to this long after the crossing is in time
    "tick_s": 4,  # the numbers move this often
    "quiet_s": None,  # set (the quiet task): no crossing before this; one comes after it
    "report_after_s": None,  # the quiet task's "2 minutes"
}
AUTO_REFRESH_S = 5
ON_CALL = "Alan Turing"

# ─── purchase orders ──────────────────────────────────────────────────────
VENDORS = ["Basalt Supply", "Cobalt Parts", "Dunmore Freight"]
CAP = 400.0
LEAD = "Grace Hopper"
LEAD_MESSAGE = (
    "Change of plan: Basalt Supply is suspended. Move every open PO to Cobalt Parts, and no PO "
    "over $400. Keep the unit prices; if a PO would go over $400, cap its quantity at the most "
    "units that keep its total at or under $400."
)
CHAT_BEFORE = [
    {
        "who": "Alan Turing",
        "role": "Receiving",
        "at": "08:41",
        "text": "Dock 2 is closed until 11:00 for repairs, so deliveries go to dock 1.",
    },
    {
        "who": "Katherine Johnson",
        "role": "Finance",
        "at": "08:52",
        "text": "Month-end close is on Friday. Please get this week's purchase orders in today.",
    },
    {
        "who": LEAD,
        "role": "Team lead",
        "at": "08:58",
        "text": "Morning, all. Ada is placing today's restock orders.",
    },
]
CLOSED_POS = [
    {"number": "PO-24068", "item": "Stretch film, 18 in", "qty": 20, "unit_price": 16.0,
     "vendor": "Basalt Supply"},
    {"number": "PO-24069", "item": "Corner boards", "qty": 200, "unit_price": 0.85,
     "vendor": "Dunmore Freight"},
    {"number": "PO-24070", "item": "Packing tape, 6-pack", "qty": 15, "unit_price": 11.4,
     "vendor": "Basalt Supply"},
]
FIRST_PO = 24071
MESSAGE_DELAY_S = 1.5

# ─── expense form ─────────────────────────────────────────────────────────
CURRENCIES = [
    ("USD", "US dollar"),
    ("EUR", "Euro"),
    ("GBP", "British pound"),
    ("CAD", "Canadian dollar"),
    ("JPY", "Japanese yen"),
]
CATEGORIES = ["Airfare", "Lodging", "Meals", "Ground transport", "Office supplies", "Software"]
# Currency and category of each receipt, in table order. A form that keeps the
# last two values gets some rows right by itself and others wrong by one field.
RECEIPT_PLAN = [
    ("USD", "Airfare"),
    ("USD", "Lodging"),
    ("EUR", "Lodging"),
    ("EUR", "Meals"),
    ("EUR", "Meals"),
    ("GBP", "Ground transport"),
    ("GBP", "Meals"),
    ("USD", "Office supplies"),
]
MERCHANTS = {
    "Airfare": ["Sable Air", "Kitewing Airlines", "Northstar Regional"],
    "Lodging": ["Harborview Hotel", "The Lantern Inn", "Quayside Suites"],
    "Meals": ["Juniper Bistro", "Tidewater Grill", "Marlow Noodle Bar", "Saltbox Cafe",
              "Brindle Diner"],
    "Ground transport": ["Metro Cab Co", "Northgate Rail", "Bridgeline Shuttle"],
    "Office supplies": ["Pinecrest Office Supply", "Inkwell Stationers"],
}
AMOUNTS = {
    "Airfare": (280, 760),
    "Lodging": (120, 340),
    "Meals": (11, 92),
    "Ground transport": (8, 64),
    "Office supplies": (14, 120),
}
STAY_AFTER = 5  # the dialog follows this many saved expenses
STAY_S = 60

# ─── reviews ──────────────────────────────────────────────────────────────
PRODUCT = "Harbor cordless kettle"
STARS = {5: 50, 4: 32, 3: 22, 2: 19, 1: 17}  # 140 reviews, 36 with 1 or 2 stars
PAGE = 20
LOW_BATTERY = [
    ("Battery gave up in a month",
     "Fine at first, then the battery stopped holding enough for a single boil."),
    ("Dead on day ten", "The battery died on day ten and the base light just blinks now."),
    ("Not worth the price",
     "Battery life is nowhere near the box claim. Two cups and it is flat."),
    ("Disappointed", "The battery swelled and now the lid will not close properly."),
    ("Returned it twice", "The replacement had the same battery fault as the first one."),
    ("Drains overnight", "Left it full at night and the battery was empty by morning."),
    ("Too weak to boil", "It takes ages to boil because the battery sags halfway through."),
    ("Avoid", "The battery quit after a few trips, and support was slow to reply."),
    ("Poor battery", "It barely heats one mug before the battery runs out."),
    ("Weak point", "Nice design, but the battery is the weak point and it shows fast."),
    ("Started strong", "Good for a week, then the battery would not last a full boil."),
    ("Says full, is not", "The battery light says full but it shuts off mid boil."),
    ("Gave up while camping", "Took it camping and the battery quit on the second morning."),
    ("Second failure", "Second battery failure in three months. I am done with this one."),
    ("Costly to fix", "The battery wore out fast and a new one costs half the kettle."),
]
LOW_OTHER = [
    ("Lid hinge snapped", "The lid hinge snapped on day three. Cheap plastic."),
    ("Leaky spout", "It drips down the side on every single pour."),
    ("Handle too hot", "The handle gets too hot to hold without a cloth."),
    ("Plastic taste", "The water still tastes of plastic after a dozen rinses."),
    ("So slow", "It takes almost ten minutes to boil a full jug."),
    ("Dented on arrival", "It arrived with a dented base and a cracked window."),
    ("Loud", "It rattles and hisses loud enough to wake the house."),
    ("Scale everywhere", "Limescale builds up within a week, even with filtered water."),
    ("Wobbly", "It never sits flat on the base, so it rocks when you pour."),
    ("Lid pops open", "The lid pops open while boiling and steam escapes."),
    ("Cracked window", "The water window cracked after the first month."),
    ("Lost filter", "The mesh filter fell out and got lost within days."),
    ("Smaller than shown", "It holds less than it says. Two mugs at most."),
    ("Hard to clean", "The narrow opening makes it impossible to scrub inside."),
    ("Rust spots", "Small rust spots appeared inside the jug after a few weeks."),
    ("Switch broke", "The on switch stopped clicking into place."),
    ("Overpriced", "It costs twice as much as my old kettle and does less."),
    ("Burnt smell", "There is a burnt smell every time it heats, even after a month."),
    ("Steam burns", "Steam vents straight onto your hand at the handle."),
    ("Peeling paint", "The paint peeled off near the spout within weeks."),
    ("Will not switch off", "It does not switch off when it boils. Scary."),
    ("Lukewarm", "It hisses for minutes and the water is still only warm."),
    ("Wrong color", "I ordered slate and got a faded grey one with scuffs."),
    ("Seal split", "The seal on the lid split, and now it leaks steam."),
    ("Slow support", "Three weeks to get an answer about a broken spout."),
    ("Fell apart", "Fine for a weekend, but it fell apart with daily use."),
]
OTHER_BATTERY = {  # reviews with 3 or more stars that mention the battery
    3: [
        ("Okay overall", "The battery is decent, but the lid sticks now and then."),
        ("Mixed feelings", "It boils fast. The battery could last longer for the price."),
        ("Average", "The battery lasts about a week of morning tea; the spout drips a bit."),
    ],
    4: [
        ("Good travel kettle", "The battery lasts a whole weekend of camping."),
        ("Handy at my desk", "No socket needed; the battery covers my tea all day."),
        ("Solid", "Battery life is good and it boils quickly."),
        ("Still going", "The battery is still strong after six months."),
    ],
    5: [
        ("Perfect for the van", "The battery runs four boils between top-ups. Brilliant."),
        ("Love it", "Great battery and a lovely clean pour."),
        ("Beats my old one", "The battery life beats my old kettle easily."),
        ("Best purchase this year", "The battery lasts and the base looks great on the counter."),
        ("Quiet and quick", "Quiet, quick, and the battery holds up well."),
    ],
}
MID_TITLES = ["It is fine", "Okay", "Average kettle", "Could be better", "Middle of the road",
              "Not bad"]
MID_OPENERS = ["Does the job", "Works fine", "It is okay", "Decent kettle", "Not bad",
               "Boils well enough"]
MID_DETAILS = ["but the lid is stiff.", "but it is a bit loud.", "but the spout drips sometimes.",
               "but the handle feels thin.", "but it is pricey.", "but the filter is fiddly.",
               "but the window fogs up.", "though the paint scratches easily."]
HIGH_TITLES = ["Great kettle", "Very happy", "Does what it says", "Lovely design", "Recommended",
               "Works well", "Good value", "Happy with it", "Nice and quick", "As described",
               "Would buy again", "Solid choice"]
HIGH_OPENERS = ["Boils fast", "Looks great on the counter", "Pours cleanly", "Easy to fill",
                "Quieter than my old one", "Light and easy to carry", "Feels well made",
                "Simple to use", "Heats a mug in no time", "A good size for two"]
HIGH_DETAILS = ["and the lid opens wide.", "and the handle stays cool.",
                "and the window is easy to read.", "and it is easy to clean.",
                "and the base is compact.", "and it switches off on time.",
                "with no plastic taste.", "and the color is lovely.", "and the price was fair.",
                "and it arrived quickly."]
AUTHORS = ["Maya R.", "Tom B.", "Priya S.", "Leo M.", "Hana K.", "Omar F.", "Ines D.", "Felix W.",
           "Rosa P.", "Ken T.", "Nadia L.", "Sam O.", "Yuki H.", "Ben C.", "Lena G.", "Raj V.",
           "Chloe N.", "Ivan Z.", "Mia J.", "Theo A."]


# ─── episode state ────────────────────────────────────────────────────────


def init(ep: Episode) -> None:
    cfg = ep.config
    q = dict(DEFAULT_QUIRKS)
    q.update(cfg.get("quirks") or {})
    cfg["quirks"] = q
    seed = cfg.get("seed", 11)
    ep.state.update(
        {
            "seed": seed,
            "queue": _timeline(cfg, seed),
            "orders": {
                "pos": [],
                "next": FIRST_PO,
                "created": 0,
                # With the quirk off the message is in the chat from the start.
                "message_due": None if q["late_message"] else 0.0,
                "message_seen": None,
                "delay_s": float(cfg.get("message_delay_s", MESSAGE_DELAY_S)),
            },
            "expenses": {
                "receipts": _receipts(seed),
                "saved": [],
                "requests": 0,
                "stay_s": int(cfg.get("stay_s", STAY_S)),
                "prompted": None,
                "stayed": None,
                "timed_out": None,
            },
            "reviews": _reviews(seed),
        }
    )
    EPISODES[ep.id] = ep


def _clock(t: float) -> str:
    s = SHIFT_START_S + int(t)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def _timeline(cfg: dict[str, Any], seed: Any) -> dict[str, Any]:
    """The support backlog over the episode: a seeded start value, noise within
    3%, optional near misses just under the +10% line, and the crossing."""
    tl = {**TIMELINE, **(cfg.get("timeline") or {})}
    rng = random.Random(f"{seed}-backlog")
    b0 = rng.randint(380, 460)
    line = -(-b0 * 11 // 10)  # the least whole number of tickets at least 10% above b0
    tick = float(tl["tick_s"])
    quiet = tl.get("quiet_s") is not None
    if quiet:
        watch = float(tl.get("report_after_s") or tl["quiet_s"])
        cross = round(float(tl["quiet_s"]) + rng.uniform(5.0, 15.0), 2)
        misses = [(0.30 * watch, 0.45 * watch), (0.62 * watch, 0.76 * watch)]
    else:
        lo, hi = (float(x) for x in tl["cross_s"])
        watch = None
        cross = round(rng.uniform(lo, hi), 2)
        misses = [(0.45 * cross, 0.75 * cross)]
    misses = [(round(a, 2), round(b, 2)) for a, b in misses] if cfg["quirks"]["near_miss"] else []
    end = cross + float(tl["window_s"]) + 900.0
    at = {round(i * tick, 2) for i in range(1, int(end / tick) + 1)}
    at |= {cross, *(x for m in misses for x in m)}
    points = [[0.0, b0]]
    for t in sorted(at):
        if t >= cross:
            v = line + rng.randint(1, 4) + min(b0 // 6, int((t - cross) / tick))
        elif any(a <= t < b for a, b in misses):
            v = line - rng.randint(1, 2)
        else:
            v = b0 + round(b0 * rng.uniform(-0.03, 0.03))
        points.append([t, v])
    return {
        "b0": b0,
        "threshold": line,
        "cross_t": cross,
        "window_s": float(tl["window_s"]),
        "tick_s": tick,
        "quiet": quiet,
        "report_after_s": watch,
        "near_misses": [list(m) for m in misses],
        "points": points,
        "times": [p[0] for p in points],
        "pages": [],
        "paged": None,
    }


def backlog_at(q: dict[str, Any], t: float) -> int:
    i = bisect.bisect_right(q["times"], t) - 1
    return int(q["points"][max(0, i)][1])


def _tiles(ep: Episode, t: float) -> list[dict[str, Any]]:
    q = ep.state["queue"]
    r = random.Random(f"{ep.state['seed']}-tiles-{int(t // q['tick_s'])}")
    return [
        {"id": "support", "name": "Support backlog", "value": backlog_at(q, t),
         "unit": "open tickets"},
        {"id": "billing", "name": "Billing backlog", "value": 141 + r.randint(-5, 5),
         "unit": "open tickets"},
        {"id": "returns", "name": "Returns backlog", "value": 77 + r.randint(-4, 4),
         "unit": "open tickets"},
        {"id": "reply", "name": "Median first reply", "value": 14 + r.randint(-2, 3),
         "unit": "minutes"},
        {"id": "agents", "name": "Agents online", "value": 23 + r.randint(-2, 2),
         "unit": "agents"},
        {"id": "sla", "name": "At risk of missing SLA", "value": 9 + r.randint(-3, 3),
         "unit": "tickets"},
    ]


def _receipts(seed: Any) -> list[dict[str, str]]:
    rng = random.Random(f"{seed}-receipts")
    pools = {c: rng.sample(names, len(names)) for c, names in MERCHANTS.items()}
    start = dt.date(2026, 9, 7)
    out = []
    for i, (cur, cat) in enumerate(RECEIPT_PLAN):
        lo, hi = AMOUNTS[cat]
        out.append(
            {
                "date": (start + dt.timedelta(days=i * 5 // 8)).isoformat(),
                "merchant": pools[cat].pop(),
                "amount": f"{rng.uniform(lo, hi):.2f}",
                "currency": cur,
                "category": cat,
            }
        )
    return out


def mentions_battery(review: dict[str, Any]) -> bool:
    return "battery" in f"{review['title']} {review['body']}".lower()


def _reviews(seed: Any) -> dict[str, Any]:
    """140 reviews, newest first. The 1- and 2-star ones that mention the
    battery are spread over the pages, a few on the last one."""
    rng = random.Random(f"{seed}-reviews")
    n_low = rng.randint(11, 15)
    stars: list[int] = []
    low_batt: set[int] = set()
    for _ in range(200):
        stars = [s for s, n in STARS.items() for _ in range(n)]
        rng.shuffle(stars)
        low = [i for i, s in enumerate(stars) if s <= 2]
        low_batt = set(rng.sample(low, n_low))
        first = sum(1 for i in low_batt if i < PAGE)
        middle = sum(1 for i in low_batt if 3 * PAGE <= i < 6 * PAGE)
        last = sum(1 for i in low_batt if i >= 6 * PAGE)
        if first <= 3 and middle >= 3 and last >= 2:
            break
    texts_low_b = rng.sample(LOW_BATTERY, n_low)
    texts_low_o = rng.sample(LOW_OTHER, sum(1 for s in stars if s <= 2) - n_low)
    other_batt = {
        s: set(rng.sample([i for i, x in enumerate(stars) if x == s], len(OTHER_BATTERY[s])))
        for s in OTHER_BATTERY
    }
    other_texts = {s: rng.sample(OTHER_BATTERY[s], len(OTHER_BATTERY[s])) for s in OTHER_BATTERY}
    used: set[str] = set()

    def combo(openers: list[str], details: list[str]) -> str:
        while True:
            s = f"{rng.choice(openers)} {rng.choice(details)}"
            if s not in used:
                used.add(s)
                return s

    day = dt.date(2026, 10, 12)
    items = []
    metas: set[tuple[int, str, str]] = set()
    for i, s in enumerate(stars):
        if s <= 2:
            title, body = (texts_low_b if i in low_batt else texts_low_o).pop()
        elif i in other_batt[s]:
            title, body = other_texts[s].pop()
        elif s == 3:
            title, body = rng.choice(MID_TITLES), combo(MID_OPENERS, MID_DETAILS)
        else:
            title, body = rng.choice(HIGH_TITLES), combo(HIGH_OPENERS, HIGH_DETAILS)
        day -= dt.timedelta(days=rng.randint(0, 2))
        date = f"{day:%b} {day.day}, {day.year}"
        # Each rating line is unique, so no two reviews share their first line.
        author = rng.choice(AUTHORS)
        while (s, author, date) in metas:
            author = rng.choice(AUTHORS)
        metas.add((s, author, date))
        items.append(
            {"id": i + 1, "stars": s, "title": title, "body": body, "author": author, "date": date}
        )
    answer = sum(1 for r in items if r["stars"] <= 2 and mentions_battery(r))
    assert answer == n_low, "a review outside the battery lists mentions the battery"
    return {"items": items, "answer": answer, "served": []}


# ─── what the checks read ─────────────────────────────────────────────────


def view(ep: Episode) -> dict[str, Any]:
    st = ep.state
    q, o, x, r = st["queue"], st["orders"], st["expenses"], st["reviews"]
    return {
        "quirks": ep.config["quirks"],
        "queue": {
            k: q[k]
            for k in (
                "b0",
                "threshold",
                "cross_t",
                "window_s",
                "quiet",
                "report_after_s",
                "near_misses",
                "pages",
            )
        },
        "orders": {
            "pos": [dict(p, saves=list(p["saves"])) for p in o["pos"]],
            "message_due": o["message_due"],
            "message_seen": o["message_seen"],
        },
        "expenses": {
            "receipts": x["receipts"],
            "saved": [dict(s) for s in x["saved"]],
            "prompted": x["prompted"],
            "stayed": x["stayed"],
            "timed_out": x["timed_out"],
        },
        "reviews": {
            "total": len(r["items"]),
            "answer": r["answer"],
            "served": sorted(set(r["served"])),
        },
    }


# ─── parsing what the page sends ──────────────────────────────────────────


def _number(v: Any) -> float | None:
    s = re.sub(r"[\s$€£,]", "", str(v if v is not None else ""))
    try:
        x = float(s)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _date(v: Any) -> str | None:
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if not m:
            return None
        mo, d, y = (int(g) for g in m.groups())
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def _po_fields(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    item = " ".join(str(body.get("item") or "").split())
    qty = _number(body.get("qty"))
    price = _number(body.get("unit_price"))
    vendor = str(body.get("vendor") or "").strip()
    errs = []
    if not item:
        errs.append("Enter an item.")
    if qty is None or qty < 1 or qty != int(qty):
        errs.append("Quantity must be a whole number of at least 1.")
    if price is None or price <= 0:
        errs.append("Enter a unit price above $0.")
    if vendor not in VENDORS:
        errs.append("Choose a vendor.")
    if errs or qty is None or price is None:
        return None, " ".join(errs)
    n, p = int(qty), round(price, 2)
    return {"item": item, "qty": n, "unit_price": p, "vendor": vendor, "total": round(n * p, 2)}, ""


def _expense_fields(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    date = _date(body.get("date"))
    merchant = " ".join(str(body.get("merchant") or "").split())
    amount = _number(body.get("amount"))
    currency = str(body.get("currency") or "")
    category = str(body.get("category") or "")
    errs = []
    if date is None:
        errs.append("Enter the date as YYYY-MM-DD.")
    if not merchant:
        errs.append("Enter the merchant.")
    if amount is None or amount <= 0:
        errs.append("Enter an amount above 0.")
    if currency not in {c for c, _ in CURRENCIES}:
        errs.append("Choose a currency.")
    if category not in CATEGORIES:
        errs.append("Choose a category.")
    if errs or amount is None:
        return None, " ".join(errs)
    return {
        "date": date,
        "merchant": merchant,
        "amount": round(amount, 2),
        "currency": currency,
        "category": category,
    }, ""


# ─── the page API ─────────────────────────────────────────────────────────


def backend(ep: Episode, method: str, name: str, query: dict[str, str], body: Any) -> Response:
    st = ep.state
    q = ep.config["quirks"]
    body = body if isinstance(body, dict) else {}
    t = time.time() - ep.started
    if name == "session":
        return Response(200, {"quirks": q, "user": USER, "auto_refresh_s": AUTO_REFRESH_S})
    if name == "queue":
        tiles = _tiles(ep, t)
        ep.log("queue_read", why=query.get("why", ""), backlog=tiles[0]["value"])
        return Response(
            200,
            {
                "clock": _clock(t),
                "tiles": tiles,
                "on_call": ON_CALL,
                "paged": st["queue"]["paged"],
                "auto_refresh_s": 0 if q["manual_refresh"] else AUTO_REFRESH_S,
            },
        )
    if name == "orders":
        o = st["orders"]
        return Response(
            200,
            {
                "pos": [_po_public(p) for p in o["pos"]],
                "closed": [
                    {**p, "total": round(p["qty"] * p["unit_price"], 2), "status": "Received"}
                    for p in CLOSED_POS
                ],
                "vendors": VENDORS,
            },
        )
    if name == "chat":
        o = st["orders"]
        msgs = [dict(m) for m in CHAT_BEFORE]
        due = o["message_due"]
        if due is not None and t >= due:
            msgs.append(
                {"who": LEAD, "role": "Team lead", "at": _clock(due)[:5], "text": LEAD_MESSAGE,
                 "new": True}
            )
            if o["message_seen"] is None:
                o["message_seen"] = round(t, 2)
                ep.log("lead_message_shown")
        return Response(200, {"channel": "#purchasing", "messages": msgs})
    if name == "expenses":
        x = st["expenses"]
        return Response(
            200,
            {
                "receipts": x["receipts"],
                "saved": x["saved"],
                "currencies": [{"code": c, "name": n} for c, n in CURRENCIES],
                "categories": CATEGORIES,
            },
        )
    if name == "reviews":
        r = st["reviews"]
        try:
            off = max(0, int(query.get("offset") or 0))
        except ValueError:
            off = 0
        items = r["items"][off : off + PAGE]
        r["served"].append(off)
        ep.log("reviews_page", offset=off, count=len(items))
        return Response(
            200,
            {
                "product": PRODUCT,
                "total": len(r["items"]),
                "items": items,
                "summary": {
                    "average": f"{sum(x['stars'] for x in r['items']) / len(r['items']):.1f}",
                    "histogram": [[s, STARS[s]] for s in (5, 4, 3, 2, 1)],
                },
            },
        )
    if method != "POST":
        return Response(405, {"error": "POST only"})

    # ─── state changes ──────────────────────────────────────────────
    if name == "page_oncall":
        qs = st["queue"]
        qs["pages"].append(round(t, 2))
        first = len(qs["pages"]) == 1
        if first:
            qs["paged"] = {"who": ON_CALL, "clock": _clock(t)}
        ep.log("oncall_paged", backlog=backlog_at(qs, t), first=first)
        return Response(200, {"ok": True, "paged": qs["paged"]})
    if name == "po" or name.startswith("po/"):
        o = st["orders"]
        f, err = _po_fields(body)
        if f is None:
            ep.log("po_invalid", error=err)
            return Response(400, {"error": err})
        if name == "po":
            po = {"id": o["next"], "number": f"PO-{o['next']}", **f, "status": "Open",
                  "saves": [round(t, 2)]}
            o["next"] += 1
            o["pos"].append(po)
            o["created"] += 1
            if o["message_due"] is None and o["created"] >= 2:
                o["message_due"] = t + o["delay_s"]
        else:
            m = re.fullmatch(r"po/(\d+)", name)
            po = next((p for p in o["pos"] if m and p["id"] == int(m.group(1))), None)
            if po is None:
                return Response(404, {"error": "No open purchase order with that number."})
            po.update(f)
            po["saves"].append(round(t, 2))
        ep.log("po_saved", po=po["number"], edit=name != "po", **f)
        return Response(200, {"ok": True, "po": _po_public(po)})
    if name == "expense":
        x = st["expenses"]
        n = x["requests"]
        x["requests"] += 1
        delay = (
            random.Random(f"{st['seed']}-save-{n}").uniform(2.0, 4.0) if q["slow_save"] else 0.3
        )
        f, err = _expense_fields(body)
        if f is None:
            ep.log("expense_invalid", error=err)
            return Response(400, {"error": err}, delay_s=delay)
        rec = {"id": len(x["saved"]) + 1, **f}
        x["saved"].append(rec)
        ep.log("expense_saved", **rec)
        prompt = (
            bool(q["timeout_dialog"]) and len(x["saved"]) >= STAY_AFTER and x["prompted"] is None
        )
        if prompt:
            x["prompted"] = round(t, 2)
            ep.log("stay_prompt", seconds=x["stay_s"])
        return Response(
            200,
            {
                "ok": True,
                "saved": rec,
                "count": len(x["saved"]),
                "stay_prompt": {"seconds": x["stay_s"]} if prompt else None,
            },
            delay_s=delay,
        )
    if name == "stay":
        st["expenses"]["stayed"] = round(t, 2)
        ep.log("stay_signed_in")
        return Response(200, {"ok": True})
    if name == "session_timeout":  # the page's countdown ran out and it cleared the form
        st["expenses"]["timed_out"] = round(t, 2)
        ep.log("session_timeout")
        return Response(200, {"ok": True})
    return Response(404, {"error": f"no route {name}"})


def _po_public(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p[k] for k in ("id", "number", "item", "qty", "unit_price", "total", "vendor",
                              "status")}
