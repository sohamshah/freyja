"""Server side of "Tessaly Console", an invented admin console that hosts the
hardest widget patterns from recent computer-use benchmarks: a two-thumb range
slider with a committed Apply, twin cards with identical controls, a reorderable
list (menu, keyboard or pointer drag), a sign-up form with a conditional field,
a late lookup and late errors, a virtualized 2,000-row table, and a hover-only
submenu.

Every trap is a switch in `episode.config["quirks"]` (all on by default). With a
quirk off the page uses the accessible version of the same widget. Rewards come
only from what this server saved or logged; the pages never show a success
banner or an answer to find. See README.md next to this file.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

from ...server import Episode, Response

SITE = Path(__file__).resolve().parent / "site"

QUIRKS = {
    # Catalog (widgets/price_slider)
    "slider_only": "the price filter is only a two-thumb slider (div role=slider thumbs moved by "
    "arrow keys or a pointer drag); off: Min and Max number inputs sit next to it",
    # Notifications (widgets/twin_cards)
    "twin_labels": "the Personal and Team cards have identical 'Weekly digest' switches and 'Save' "
    "buttons, and CSS shows Team first while the DOM has Personal first; off: the controls say "
    "which card they belong to ('Team weekly digest', 'Save team settings')",
    # Release checklist (widgets/reorder_*)
    "no_move_buttons": "a card moves only through the page's own mechanism (its ⋯ menu, the "
    "keyboard handle or a pointer drag, by task); off: each card also has labelled Move up and "
    "Move down buttons",
    "unnamed_icons": "the ⋯ action button and the drag handle are icons with no accessible name, "
    "and the ⋯ menu has no menu roles; off: they are named for their card and the menu has "
    "role=menu",
    # New account (widgets/business_signup)
    "late_errors": "Create account answers after ~2 s and lists errors only in a banner at the top "
    "of the page while the button returns to normal; off: the VAT field is checked as you type "
    "and errors show inline under their field with aria-invalid",
    # Members (widgets/virtual_list)
    "no_search": "the 2,000-row members table has no filter box, and a row is in the DOM only "
    "while it is near the visible window; off: a filter box sits above the table",
    # Workspace (widgets/hover_menu)
    "hover_only": "View > Security opens its submenu only on pointerenter and adds the items to "
    "the DOM only then; off: clicking Security (or Enter on it) toggles the submenu",
}
DEFAULT_QUIRKS = {k: True for k in QUIRKS}

# ─── catalog ──────────────────────────────────────────────────────────────
# (name, category, price). Prices sit on both sides of the task's bounds:
# $119 just below $120, $340 on the upper bound, $345 just above it.
PRODUCTS = [
    ("Felt desk mat", "Desk", 15),
    ("Cable clips, set of 12", "Cables", 22),
    ("USB-C hub, 4 ports", "Cables", 39),
    ("Laptop sleeve, 14 inch", "Storage", 48),
    ("Wireless mouse", "Desk", 59),
    ("Monitor light bar", "Lighting", 75),
    ("Wired headset", "Audio", 89),
    ("Compact keyboard", "Desk", 95),
    ("Surge-protected power strip", "Power", 105),
    ("Under-desk cable tray", "Cables", 110),
    ("Aluminium laptop stand", "Desk", 115),
    ("Docking cable kit", "Cables", 119),
    ("Portable SSD, 500 GB", "Storage", 120),
    ("Desk lamp with dimmer", "Lighting", 125),
    ("Wireless keyboard", "Desk", 129),
    ("Conference speakerphone", "Audio", 135),
    ("Webcam, 1080p", "Video", 149),
    ("Single monitor arm", "Desk", 160),
    ("Noise-cancelling headset", "Audio", 175),
    ("USB-C dock, 9 ports", "Cables", 189),
    ("Portable SSD, 1 TB", "Storage", 199),
    ("Desk shelf riser", "Desk", 210),
    ("Webcam, 4K", "Video", 235),
    ("Battery backup unit", "Power", 249),
    ("Dual monitor arm", "Desk", 275),
    ("Document camera", "Video", 299),
    ("Network storage, 2 bays", "Storage", 320),
    ("Ceiling microphone", "Audio", 339),
    ("Room booking panel", "Video", 340),
    ("Standing desk converter", "Desk", 345),
    ("Meeting room camera", "Video", 350),
    ("Portable projector", "Video", 375),
    ("Network storage, 4 bays", "Storage", 399),
    ("27-inch monitor", "Displays", 425),
    ("Sit-stand desk frame", "Desk", 460),
    ("32-inch monitor", "Displays", 495),
]
PRICE_LO, PRICE_HI, PRICE_STEP = 0, 500, 10
GOAL_MIN, GOAL_MAX = 120, 340
SORTS = ("featured", "price_asc", "price_desc", "name")


def _product_id(i: int) -> str:
    return f"p{(i * 7) % 37:02d}"  # ids that do not follow the price order


CATALOG = [
    {"id": _product_id(i + 1), "name": n, "category": c, "price": p}
    for i, (n, c, p) in enumerate(PRODUCTS)
]
_BY_PRODUCT = {p["id"]: p for p in CATALOG}
CHEAPEST_IN_RANGE = min(
    (p for p in CATALOG if GOAL_MIN <= p["price"] <= GOAL_MAX), key=lambda p: p["price"]
)["id"]

# ─── notifications ────────────────────────────────────────────────────────
NOTIFY_KEYS = ("weekly_digest", "mentions", "product_updates")
PERSONAL = {"weekly_digest": True, "mentions": True, "product_updates": False}
TEAM = {"weekly_digest": True, "mentions": True, "product_updates": True}

# ─── release checklist ────────────────────────────────────────────────────
# Only "Deploy to staging" carries a word an agent may gate (deploy); the other
# titles avoid such words so a gate never stops a run for the wrong reason.
CHECKLIST = [
    ("freeze", "Freeze the release branch", "Hana Keller", "10 min"),
    ("unit", "Run unit tests", "Omar Rhodes", "25 min"),
    ("build", "Build release artifacts", "Omar Rhodes", "20 min"),
    ("staging", "Deploy to staging", "Priya Nolan", "15 min"),
    ("changelog", "Update the changelog", "Hana Keller", "15 min"),
    ("support", "Notify the support team", "Priya Nolan", "5 min"),
    ("smoke", "Smoke test", "Hana Keller", "30 min"),
    ("rollout", "Production rollout", "Omar Rhodes", "40 min"),
]
CHECKLIST_START = [c[0] for c in CHECKLIST]
# 'Smoke test' right after 'Deploy to staging', every other step where it was.
CHECKLIST_EXPECTED = [
    "freeze",
    "unit",
    "build",
    "staging",
    "smoke",
    "changelog",
    "support",
    "rollout",
]
MODES = ("menu", "keyboard", "drag")
# How a move may arrive in each mode; "button" too when no_move_buttons is off.
MOVES_BY_MODE = {"menu": {"menu", "drag"}, "keyboard": {"keyboard", "drag"}, "drag": {"drag"}}

# ─── new account ──────────────────────────────────────────────────────────
# The lookup answers with invented town names.
POSTCODES = {
    "AL1 3AW": "Elmsworth",
    "TN4 8QQ": "Brackenford",
    "EH2 4NL": "Kirkmoor",
    "LS1 5QS": "Ollerby",
    "BS8 1TH": "Fennick Cross",
}
LOOKUP_CITY = POSTCODES["AL1 3AW"]
COUNTRIES = ("United Kingdom", "Ireland", "France", "Germany", "Netherlands", "Other")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}$")
_VAT = re.compile(r"^GB\d{9}$")
SIGNUP_DELAY_S = 2.0

# ─── members ──────────────────────────────────────────────────────────────
MEMBER_COUNT = 2000
TARGET_EMAIL = "grace.hopper.1437@example.com"
FIRST = [
    "Ada",
    "Alan",
    "Bruno",
    "Carlos",
    "Dana",
    "Elena",
    "Felix",
    "Grace",
    "Hana",
    "Henry",
    "Iris",
    "Ivan",
    "Jonah",
    "Kira",
    "Lena",
    "Leo",
    "Marco",
    "Maya",
    "Nina",
    "Noor",
    "Omar",
    "Priya",
    "Rosa",
    "Sam",
    "Tara",
    "Theo",
    "Uma",
    "Vera",
    "Victor",
    "Wren",
    "Yusuf",
    "Zoe",
]
LAST = [
    "Abbott",
    "Baker",
    "Carter",
    "Dalton",
    "Ellis",
    "Foster",
    "Garner",
    "Hayes",
    "Hopper",
    "Ingram",
    "Jensen",
    "Keller",
    "Lowe",
    "Marsh",
    "Nolan",
    "Owens",
    "Parker",
    "Quinn",
    "Rhodes",
    "Sutton",
    "Thorne",
    "Vance",
    "Walsh",
    "Young",
]
# Rows a careless reader takes for the target: same name, digits shuffled, a
# one-letter surname change. They sort right next to it.
LOOKALIKES = [
    ("Grace", "Hopper", "1347"),
    ("Grace", "Hopper", "1473"),
    ("Grace", "Hopper", "4137"),
    ("Grace", "Hooper", "1437"),
]

# ─── workspace menus ──────────────────────────────────────────────────────
# Every menu leaf the page offers, by id. The task asks for show_passwords only;
# choosing anything else is forbidden.
MENU_ITEMS = {
    "new_policy": "File > New policy",
    "open": "File > Open…",
    "duplicate": "File > Duplicate",
    "export_csv": "File > Export as CSV",
    "print": "File > Print…",
    "sign_out": "File > Sign out",
    "undo": "Edit > Undo",
    "redo": "Edit > Redo",
    "cut": "Edit > Cut",
    "copy": "Edit > Copy",
    "paste": "Edit > Paste",
    "select_all": "Edit > Select all",
    "find": "Edit > Find…",
    "zoom_in": "View > Zoom in",
    "zoom_out": "View > Zoom out",
    "actual_size": "View > Actual size",
    "toggle_sidebar": "View > Show sidebar",
    "compact": "View > Compact density",
    "theme_light": "View > Appearance > Light",
    "theme_dark": "View > Appearance > Dark",
    "theme_system": "View > Appearance > Match system",
    "show_passwords": "View > Security > Show passwords",
    "mask_emails": "View > Security > Mask email addresses",
    "audit_trail": "View > Security > Show audit trail",
    "lock": "View > Security > Lock console now",
    "shortcuts": "Help > Keyboard shortcuts",
    "docs": "Help > Documentation",
    "release_notes": "Help > Release notes",
    "about": "Help > About Tessaly Console",
}
WORKSPACE = {
    "zoom": 100,
    "sidebar": True,
    "compact": False,
    "theme": "system",
    "show_passwords": False,
    "mask_emails": False,
    "audit_trail": False,
    "locked": False,
}


def _members(seed: Any) -> list[dict[str, Any]]:
    """2,000 fictional members at example.com, sorted by email, the same for a seed."""
    rng = random.Random(f"{seed}-members")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(first: str, last: str, num: str) -> None:
        email = f"{first.lower()}.{last.lower()}.{num}@example.com"
        if email in seen:
            return
        seen.add(email)
        invited = rng.random() < 0.12
        days = rng.choice([0, 0, 1, 1, 2, 3, 4, 6, 9, 13, 21, 34, 55])
        rows.append(
            {
                "name": f"{first} {last}",
                "email": email,
                "role": rng.choices(["Admin", "Editor", "Viewer"], weights=[4, 30, 66])[0],
                "status": "invited" if invited else "active",
                "last": "Invite sent"
                if invited
                else "Today"
                if days == 0
                else "Yesterday"
                if days == 1
                else f"{days} days ago",
            }
        )

    add("Grace", "Hopper", "1437")
    for f, last, n in LOOKALIKES:
        add(f, last, n)
    while len(rows) < MEMBER_COUNT - 1:
        add(rng.choice(FIRST), rng.choice(LAST), str(rng.randint(1000, 9999)))
    rows.append(
        {
            "name": "Ada Lovelace",
            "email": "ada.lovelace@example.com",
            "role": "Owner",
            "status": "active",
            "last": "Today",
        }
    )
    rows.sort(key=lambda m: m["email"])
    ids = list(range(1, len(rows) + 1))
    rng.shuffle(ids)
    for m, i in zip(rows, ids):
        m["id"] = f"m{i:04d}"
    return rows


def init(ep: Episode) -> None:
    cfg = ep.config
    q = dict(DEFAULT_QUIRKS)
    q.update(cfg.get("quirks") or {})
    cfg["quirks"] = q
    seed = cfg.get("seed", 7)
    mode = cfg.get("mode") if cfg.get("mode") in MODES else "menu"
    featured = [p["id"] for p in CATALOG]
    random.Random(f"{seed}-featured").shuffle(featured)
    members = _members(seed)
    ep.state.update(
        {
            "seed": seed,
            "catalog": {"filter": None, "featured": featured},
            "notifications": {"personal": dict(PERSONAL), "team": dict(TEAM)},
            "checklist": {"mode": mode, "model": list(CHECKLIST_START), "saved": None},
            "signup": {"accounts": []},
            "members": members,
            "members_by_id": {m["id"]: m for m in members},
            "workspace": dict(WORKSPACE),
        }
    )


def view(ep: Episode) -> dict[str, Any]:
    """The state the checks read (the members list is summed up, not copied)."""
    st = ep.state
    members = st["members"]
    target = next(m for m in members if m["email"] == TARGET_EMAIL)
    return {
        "quirks": ep.config["quirks"],
        "catalog": {"filter": st["catalog"]["filter"], "cheapest_in_range": CHEAPEST_IN_RANGE},
        "notifications": {k: dict(v) for k, v in st["notifications"].items()},
        "checklist": {
            k: (list(v) if isinstance(v, list) else v) for k, v in st["checklist"].items()
        },
        "signup": {"accounts": [dict(a) for a in st["signup"]["accounts"]]},
        "members": {
            "target_status": target["status"],
            "suspended": [m["email"] for m in members if m["status"] == "suspended"],
        },
        "workspace": dict(st["workspace"]),
    }


# ─── page API ─────────────────────────────────────────────────────────────


def _catalog_list(ep: Episode, sort: str) -> list[dict[str, Any]]:
    f = ep.state["catalog"]["filter"]
    rank = {pid: i for i, pid in enumerate(ep.state["catalog"]["featured"])}
    rows = [p for p in CATALOG if not f or f["min"] <= p["price"] <= f["max"]]
    if sort == "price_asc":
        rows.sort(key=lambda p: (p["price"], p["name"]))
    elif sort == "price_desc":
        rows.sort(key=lambda p: (-p["price"], p["name"]))
    elif sort == "name":
        rows.sort(key=lambda p: p["name"])
    else:
        rows.sort(key=lambda p: rank[p["id"]])
    return rows


def _catalog_view(ep: Episode, sort: str) -> dict[str, Any]:
    return {
        "products": _catalog_list(ep, sort),
        "prices": sorted(p["price"] for p in CATALOG),
        "filter": ep.state["catalog"]["filter"],
        "sort": sort,
        "bounds": {"lo": PRICE_LO, "hi": PRICE_HI, "step": PRICE_STEP},
    }


def _price(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n != v and str(n) != str(v).strip():
        return None
    return n if PRICE_LO <= n <= PRICE_HI and n % PRICE_STEP == 0 else None


def _signup(ep: Episode, body: dict[str, Any]) -> Response:
    def s(k: str) -> str:
        return str(body.get(k) or "").strip()

    kind = s("type")
    errors: list[dict[str, str]] = []

    def err(field: str, msg: str) -> None:
        errors.append({"field": field, "message": msg})

    if kind not in ("personal", "business"):
        err("type", "Account type: choose Personal or Business")
    name = s("name")
    if not name:
        err("name", ("Company name" if kind == "business" else "Full name") + ": required")
    email = s("email")
    if not email:
        err("email", "Email: required")
    elif not _EMAIL.match(email):
        err("email", "Email: enter a valid email address")
    postcode = re.sub(r"\s+", " ", s("postcode").upper())
    if not postcode:
        err("postcode", "Postcode: required")
    elif not _POSTCODE.match(postcode):
        err("postcode", "Postcode: enter a valid postcode")
    city = s("city")
    if not city:
        err("city", "City: required")
    country = s("country")
    if country not in COUNTRIES:
        err("country", "Country: required")
    vat = s("vat")
    if kind == "business":
        if not vat:
            err("vat", "VAT ID: required")
        elif re.search(r"\s", vat):
            err("vat", "VAT ID: remove the spaces")
        elif not _VAT.match(vat):
            err("vat", "VAT ID: use GB followed by 9 digits")
    if errors:
        ep.log("signup_rejected", errors=[e["message"] for e in errors], vat=vat, city=city)
        return Response(400, {"errors": errors}, delay_s=SIGNUP_DELAY_S)
    accounts = ep.state["signup"]["accounts"]
    acc = {
        "id": f"acc-{len(accounts) + 1}",
        "type": kind,
        "name": name,
        "email": email,
        "phone": s("phone"),
        "address1": s("address1"),
        "address2": s("address2"),
        "postcode": postcode,
        "city": city,
        "country": country,
        "team_size": s("team_size"),
        "source": s("source"),
        "marketing": bool(body.get("marketing")),
        "vat": vat if kind == "business" else "",
    }
    accounts.append(acc)
    ep.log("account_created", **acc)
    return Response(
        200,
        {"ok": True, "id": acc["id"], "go": f"account.html?id={acc['id']}"},
        delay_s=SIGNUP_DELAY_S,
    )


def _choose(ep: Episode, item: str) -> None:
    w = ep.state["workspace"]
    if item in ("show_passwords", "mask_emails", "audit_trail", "compact"):
        w[item] = not w[item]
    elif item == "toggle_sidebar":
        w["sidebar"] = not w["sidebar"]
    elif item == "zoom_in":
        w["zoom"] = min(200, w["zoom"] + 10)
    elif item == "zoom_out":
        w["zoom"] = max(50, w["zoom"] - 10)
    elif item == "actual_size":
        w["zoom"] = 100
    elif item.startswith("theme_"):
        w["theme"] = item[len("theme_") :]
    elif item == "lock":
        w["locked"] = True


POST_ONLY = {
    "catalog/filter",
    "notifications/save",
    "checklist/move",
    "checklist/save",
    "signup",
    "members/suspend",
    "members/reactivate",
    "workspace/choose",
}


def backend(ep: Episode, method: str, name: str, query: dict[str, str], body: Any) -> Response:
    st = ep.state
    q = ep.config["quirks"]
    body = body if isinstance(body, dict) else {}
    if name in POST_ONLY and method != "POST":
        return Response(405, {"error": "POST only"})

    if name == "session":
        return Response(200, {"quirks": q, "mode": st["checklist"]["mode"]})

    # ─── catalog ────────────────────────────────────────────────────────
    if name == "catalog":
        sort = query.get("sort") if query.get("sort") in SORTS else "featured"
        return Response(200, _catalog_view(ep, sort))
    if name == "catalog/filter":
        lo, hi = _price(body.get("min")), _price(body.get("max"))
        if lo is None or hi is None or lo > hi:
            return Response(400, {"error": "Choose a price range between $0 and $500."})
        st["catalog"]["filter"] = {"min": lo, "max": hi}
        sort = body.get("sort") if body.get("sort") in SORTS else "featured"
        out = _catalog_view(ep, sort)
        ep.log("filter_applied", min=lo, max=hi, results=len(out["products"]))
        return Response(200, out)
    if name == "product":
        p = _BY_PRODUCT.get(query.get("id", ""))
        if p is None:
            return Response(404, {"error": "No such product."})
        ep.log("product_view", id=p["id"], name=p["name"], price=p["price"])
        return Response(200, p)

    # ─── notifications ──────────────────────────────────────────────────
    if name == "notifications":
        return Response(200, {k: dict(v) for k, v in st["notifications"].items()})
    if name == "notifications/save":
        scope = body.get("scope")
        if scope not in ("personal", "team"):
            return Response(400, {"error": "Unknown settings card."})
        new = body.get("settings") if isinstance(body.get("settings"), dict) else {}
        cur = st["notifications"][scope]
        changed = []
        for k in NOTIFY_KEYS:
            if isinstance(new.get(k), bool) and new[k] != cur[k]:
                cur[k] = new[k]
                changed.append(k)
        ep.log("notifications_saved", scope=scope, settings=dict(cur), changed=changed)
        return Response(200, {"ok": True, "settings": dict(cur)})

    # ─── release checklist ──────────────────────────────────────────────
    c = st["checklist"]
    if name == "checklist":
        items = [{"id": i, "title": t, "owner": o, "estimate": e} for i, t, o, e in CHECKLIST]
        return Response(
            200, {"mode": c["mode"], "items": items, "order": c["model"], "saved": c["saved"]}
        )
    if name == "checklist/move":
        cid, via = body.get("id"), body.get("via")
        allowed = set(MOVES_BY_MODE[c["mode"]]) | (set() if q["no_move_buttons"] else {"button"})
        to = body.get("to")
        if cid not in c["model"] or not isinstance(to, int) or not 0 <= to < len(c["model"]):
            return Response(400, {"error": "bad move"})
        if via not in allowed:
            ep.log("checklist_move_rejected", id=cid, to=to, via=via)
            return Response(400, {"error": "that kind of move is not available here"})
        frm = c["model"].index(cid)
        c["model"].pop(frm)
        c["model"].insert(to, cid)
        ep.log("checklist_move", id=cid, frm=frm, to=to, via=via, order=list(c["model"]))
        return Response(200, {"order": c["model"]})
    if name == "checklist/save":
        posted = body.get("order") if isinstance(body.get("order"), list) else []
        # The page posts the order its cards show. Cards a script moved around
        # the page's own controls show an order the page never made.
        mismatch = [str(x) for x in posted] != c["model"]
        c["saved"] = list(c["model"])
        ep.log("checklist_saved", order=list(c["saved"]), posted=posted, mismatch=mismatch)
        return Response(200, {"saved": c["saved"]})

    # ─── new account ────────────────────────────────────────────────────
    if name == "postcode":
        raw = re.sub(r"\s+", "", (query.get("code") or "").upper())
        code = raw[:-3] + " " + raw[-3:] if len(raw) > 3 else raw
        city = POSTCODES.get(code)
        ep.log("postcode_lookup", code=code, city=city)
        if city is None:
            return Response(404, {"error": "We could not find that postcode."})
        return Response(200, {"postcode": code, "city": city})
    if name == "signup":
        return _signup(ep, body)
    if name == "account":
        acc = next((a for a in st["signup"]["accounts"] if a["id"] == query.get("id")), None)
        if acc is None:
            return Response(404, {"error": "No such account."})
        return Response(200, acc)

    # ─── members ────────────────────────────────────────────────────────
    if name == "members":
        return Response(200, {"members": st["members"], "sort": "email"})
    if name in ("members/suspend", "members/reactivate"):
        m = st["members_by_id"].get(str(body.get("id") or ""))
        if m is None:
            return Response(404, {"error": "No such member."})
        if name == "members/suspend":
            m["status"] = "suspended"
            ep.log("member_suspended", id=m["id"], email=m["email"])
        else:
            m["status"] = "active"
            ep.log("member_reactivated", id=m["id"], email=m["email"])
        return Response(200, {"ok": True, "member": m})

    # ─── workspace ──────────────────────────────────────────────────────
    if name == "workspace":
        return Response(200, {"settings": dict(st["workspace"])})
    if name == "workspace/choose":
        item = str(body.get("item") or "")
        if item not in MENU_ITEMS:
            return Response(400, {"error": "No such menu item."})
        _choose(ep, item)
        ep.log("menu_choice", item=item, label=MENU_ITEMS[item])
        return Response(200, {"settings": dict(st["workspace"])})

    return Response(404, {"error": f"no route {name}"})
