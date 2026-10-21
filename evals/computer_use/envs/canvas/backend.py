"""Server side of Ledger, an invented small-business dashboard whose whole
interface is drawn on one `<canvas>`: a sidebar, an orders table with a
detail drawer, and a settings page with a text field, a dropdown and three
toggles. The page has no DOM controls and no DOM text, which is what a
Flutter (CanvasKit) app, a spreadsheet grid or a design tool looks like to
an accessibility reader: an operator run gets nothing to read and must act
from screenshots.

Every pattern is a quirk in `episode.config["quirks"]`, on by default; with
a quirk off the page draws the plain version. Rewards read only this
module's state and events.

Config keys, all optional:
  quirks   {name: bool}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...server import Episode, Response

SITE = Path(__file__).resolve().parent / "site"

QUIRKS = {
    "small_controls":
        "toggles are 34 by 18 pixels and table rows 36 pixels tall, the sizes a dense dashboard "
        "draws; a grounding box has to land inside them",
    "confirm_sheet":
        "marking an order shipped opens a confirmation sheet with Ship and Keep open, so the "
        "action takes two clicks and the second target is new on screen",
    "same_customer":
        "two open orders belong to the same customer, so a target named by customer alone is "
        "ambiguous and the order number is what tells them apart",
}
DEFAULT_QUIRKS = {k: True for k in QUIRKS}

BUSINESS = "Harbor Lane Goods"
TIMEZONES = [
    "America/Los_Angeles",
    "America/New_York",
    "Europe/London",
    "Europe/Lisbon",
    "Europe/Berlin",
    "Asia/Tokyo",
]
DEFAULT_SETTINGS = {
    "name": BUSINESS,
    "timezone": "America/Los_Angeles",
    "email": True,
    "sms": False,
    "digest": False,
}

# (id, customer, date, amount in cents, status). Amounts are in cents so the
# open total is exact.
ORDERS = [
    (1041, "Jonah Petrakis", "2026-10-14", 41200, "open"),
    (1042, "Mara Lindqvist", "2026-10-15", 128450, "open"),
    (1043, "Dev Anand", "2026-10-15", 9620, "shipped"),
    (1044, "Mara Lindqvist", "2026-10-16", 23375, "open"),
    (1045, "Sofia Reyes", "2026-10-17", 5800, "cancelled"),
    (1046, "Tomasz Wróbel", "2026-10-18", 73000, "open"),
    (1047, "Priya Natarajan", "2026-10-19", 101500, "open"),
]
# With same_customer off, order 1044 belongs to someone else.
OTHER_CUSTOMER = "Lena Okafor"


def init(ep: Episode) -> None:
    q = dict(DEFAULT_QUIRKS)
    q.update(ep.config.get("quirks") or {})
    ep.config["quirks"] = q
    orders = []
    for oid, customer, date, cents, status in ORDERS:
        if oid == 1044 and not q["same_customer"]:
            customer = OTHER_CUSTOMER
        orders.append({"id": oid, "customer": customer, "date": date, "cents": cents, "status": status})
    ep.state.update({
        "orders": orders,
        "settings": dict(DEFAULT_SETTINGS),
        "saves": [],
        "shipped": [],
        "cancelled": [],
    })


def view(ep: Episode) -> dict[str, Any]:
    st = ep.state
    return {
        "quirks": ep.config["quirks"],
        "orders": [dict(o) for o in st["orders"]],
        "settings": dict(st["settings"]),
        "saves": [dict(s) for s in st["saves"]],
        "shipped": list(st["shipped"]),
        "cancelled": list(st["cancelled"]),
    }


def open_total_cents(st: dict[str, Any]) -> int:
    return sum(o["cents"] for o in st["orders"] if o["status"] == "open")


def dollars(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def _order(ep: Episode, body: Any) -> dict[str, Any] | None:
    try:
        oid = int((body or {}).get("id"))
    except (TypeError, ValueError):
        return None
    return next((o for o in ep.state["orders"] if o["id"] == oid), None)


def backend(ep: Episode, method: str, name: str, query: dict[str, str], body: Any) -> Response:
    st = ep.state
    if name == "state":
        return Response(body={
            "quirks": ep.config["quirks"],
            "orders": [dict(o) for o in st["orders"]],
            "settings": dict(st["settings"]),
            "timezones": TIMEZONES,
        })
    if method != "POST":
        return Response(status=405, body={"error": "POST"})
    if name == "nav":
        ep.log("nav", page=str((body or {}).get("page")))
        return Response(body={"ok": True})
    if name in ("ship", "cancel"):
        o = _order(ep, body)
        if o is None:
            return Response(status=404, body={"error": "no such order"})
        if o["status"] != "open":
            ep.log(name, id=o["id"], refused=o["status"])
            return Response(status=409, body={"error": f"order is {o['status']}"})
        o["status"] = "shipped" if name == "ship" else "cancelled"
        st["shipped" if name == "ship" else "cancelled"].append(o["id"])
        ep.log(name, id=o["id"])
        return Response(body={"ok": True, "status": o["status"]})
    if name == "save_settings":
        b = body or {}
        new = dict(st["settings"])
        if isinstance(b.get("name"), str):
            new["name"] = b["name"].strip()[:60]
        if b.get("timezone") in TIMEZONES:
            new["timezone"] = b["timezone"]
        for k in ("email", "sms", "digest"):
            if isinstance(b.get(k), bool):
                new[k] = b[k]
        changed = {k: v for k, v in new.items() if st["settings"][k] != v}
        st["settings"] = new
        st["saves"].append(changed)
        ep.log("save_settings", changed=changed)
        return Response(body={"ok": True, "settings": new})
    return Response(status=404, body={"error": f"no api {name}"})
