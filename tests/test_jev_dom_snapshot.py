"""Run bridge/tools/jev_operator/dom_snapshot.js in a real headless Chrome.

Stdlib only: a tiny CDP websocket client. Skips when no Chrome is found.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "bridge/tools/jev_operator/dom_snapshot.js").read_text()
FIXTURE = ROOT / "tests/fixtures/jev_dom/basic.html"
CHROME_CANDIDATES = [
    os.environ.get("JEV_TEST_CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]


def _chrome():
    for c in CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


class _WS:
    def __init__(self, url):
        hostport, path = url[len("ws://"):].split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(1)
        self.n = 0

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        head = b"\x81" + (bytes([0x80 | n]) if n < 126 else
                          b"\xfe" + struct.pack(">H", n) if n < 65536 else
                          b"\xff" + struct.pack(">Q", n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, k):
        b = b""
        while len(b) < k:
            c = self.s.recv(k - len(b))
            if not c:
                raise ConnectionError("closed")
            b += c
        return b

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            msg += self._read(n)
            if b1 & 0x80:
                return json.loads(msg)

    def call(self, method, **params):
        self.n += 1
        self.send({"id": self.n, "method": method, "params": params})
        while True:
            m = self.recv()
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m["result"]


class Page:
    def __init__(self, ws):
        self.ws = ws

    def eval(self, expr):
        r = self.ws.call("Runtime.evaluate", expression=expr, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    def snap(self):
        return json.loads(self.eval("window.__freyjaJev.snapshot()"))

    def act(self, id_, op, arg=None, guard=None):
        return json.loads(self.eval(
            f"window.__freyjaJev.act({json.dumps(id_)},{json.dumps(op)},{json.dumps(arg)},{json.dumps(guard)})"))


import contextlib

PATTERNS = ROOT / "tests/fixtures/jev_dom/patterns.html"
LISTS = ROOT / "tests/fixtures/jev_dom/lists.html"


@contextlib.contextmanager
def _open(fixture):
    chrome = _chrome()
    if not chrome:
        pytest.skip("no Chrome available")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    tmp = tempfile.mkdtemp(prefix="jev-chrome-")
    proc = subprocess.Popen(
        [chrome, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={tmp}",
         "--no-first-run", "--no-default-browser-check", "--window-size=800,600",
         fixture.as_uri()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2))
                pages = [t for t in tabs if t.get("type") == "page"]
                target = next((t for t in pages if fixture.name in t.get("url", "")), None)
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not target:
            pytest.skip("headless Chrome did not start")
        p = Page(_WS(target["webSocketDebuggerUrl"]))
        p.ws.call("Emulation.setDeviceMetricsOverride", width=800, height=600, deviceScaleFactor=1, mobile=False)
        for _ in range(50):
            if p.eval("document.readyState") == "complete":
                break
            time.sleep(0.1)
        p.eval(SCRIPT)
        yield p
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def page():
    with _open(FIXTURE) as p:
        yield p


@pytest.fixture()
def patterns():
    with _open(PATTERNS) as p:
        yield p


@pytest.fixture()
def lists():
    with _open(LISTS) as p:
        yield p


def _by_label(snap, label):
    return [a for a in snap["actions"] if a["label"] == label]


def test_idempotent_install(page):
    assert page.eval(SCRIPT) == "already-installed"
    before = page.snap()["mutations"]
    page.eval("document.body.setAttribute('data-x','1')")
    time.sleep(0.1)
    assert page.snap()["mutations"] - before == 1  # one observer, not two


def test_filtering_and_shape(page):
    s = page.snap()
    labels = [a["label"] for a in s["actions"]]
    assert "Secret" not in labels and "Upload" not in labels and "Ghost" not in labels
    assert not any(a["role"] == "textbox" and a["label"] == "" for a in s["actions"])
    assert "Checkout" not in labels
    assert {"label": "Checkout", "context": ""} in s["unoffered"]
    assert s["viewport"] == {"w": 800, "h": 600}
    assert "Apple pie" in s["text"] and "Ghost" not in s["text"]
    far = _by_label(s, "Far away")
    assert far and far[0]["offscreen"] == "below"
    # Controls far below the fold are offered too (nearest 250 first), so a
    # button at the bottom of a long page needs no scrolling to find.
    way = _by_label(s, "Way far")
    assert way and way[0]["offscreen"] == "below"
    assert _by_label(s, "Size")[0]["options"], "a <select> lists its options"
    assert _by_label(s, "Search")[0]["kind"] == "fill"
    assert _by_label(s, "Size")[0]["kind"] == "select"


def test_ids_stable_and_context_distinct(page):
    a = page.snap()
    b = page.snap()
    assert [x["id"] for x in a["actions"]] == [x["id"] for x in b["actions"]]
    adds = _by_label(a, "Add")
    assert len(adds) == 2 and adds[0]["id"] != adds[1]["id"]
    assert "Apple pie" in adds[0]["context"] and "Banana bread" in adds[1]["context"]
    assert adds[0]["context"] != adds[1]["context"]
    assert _by_label(a, "Search")[0]["context"] == ""


def test_fill_select_and_stale(page):
    s = page.snap()
    q = _by_label(s, "Search")[0]
    r = page.act(q["id"], "fill", {"text": "hello", "mode": "replace"}, q["guard"])
    assert r == {"ok": True, "reason": None, "readback": "hello"}
    r = page.act(q["id"], "fill", {"text": " world", "mode": "append"})
    assert r["readback"] == "hello world"
    # the old guard embeds the old value, so it is now stale
    assert page.act(q["id"], "fill", {"text": "x", "mode": "replace"}, q["guard"])["reason"] == "stale"
    size = _by_label(s, "Size")[0]
    assert page.act(size["id"], "select", "Large")["readback"] == "l"
    assert page.act(size["id"], "select", "Nope")["reason"] == "no_such_option"


def test_key_and_scroll(page):
    q = _by_label(page.snap(), "Search")[0]
    page.act(q["id"], "fill", {"text": "k", "mode": "replace"})
    assert page.act(0, "key", "Enter")["ok"] is True
    page.eval("document.activeElement.blur()")
    assert page.act(0, "key", "Enter") == {"ok": False, "reason": "no_editable_focused", "readback": None}
    r = page.act(0, "scroll", 500)
    assert r["ok"] and r["moved"] > 0 and r["readback"].startswith("page at ")
    page.act(0, "scroll", -10000)
    r = page.act(0, "scroll", -500)
    assert r["ok"] and r["moved"] == 0 and "already at the top" in r["readback"]


def test_covered_rejected_and_click(page):
    s = page.snap()
    cov = _by_label(s, "Covered")[0]
    page.eval("document.getElementById('cover').style.display='block'")
    assert page.act(cov["id"], "click", None, cov["guard"])["reason"].startswith("covered")
    page.eval("document.getElementById('cover').style.display='none'")
    page.eval("window.__clicked=0;document.getElementById('cov').addEventListener('click',()=>window.__clicked++)")
    assert page.act(cov["id"], "click", None, cov["guard"])["ok"] is True
    assert page.eval("window.__clicked") == 1


def test_shadow_dom_controls_are_listed_and_usable(page):
    """Controls inside open shadow roots are listed, labelled from their own
    root, typed into and clicked (MDN's search lived entirely in shadow DOM and
    the snapshot could not see it)."""
    s = page.snap()
    field = _by_label(s, "Find docs")
    go = _by_label(s, "Go")
    assert field and field[0]["kind"] == "fill" and field[0]["enter"] == "search"
    assert go and go[0]["kind"] == "click"
    assert "Shadow text here" in s["text"]
    r = page.act(field[0]["id"], "fill", {"text": "flat", "mode": "replace"})
    assert r["ok"] and r["readback"] == "flat"
    assert _by_label(page.snap(), "Find docs")[0]["focused"] is True
    assert page.act(go[0]["id"], "click")["ok"] is True
    assert page.eval("document.title") == "went:flat"


def test_page_dialogs_do_not_block_and_confirm_needs_permission(page):
    """While a run is active, confirm()/alert() do not block: confirm answers
    Cancel unless the run may confirm, and both are reported."""
    ask = _by_label(page.snap(), "Ask")[0]
    r = page.act(ask["id"], "click")
    assert r["ok"] and page.eval("document.title") == "declined"
    kinds = [(d["kind"], d.get("answer")) for d in r["dialogs"]]
    assert kinds == [("confirm", False), ("alert", None)] and r["dialogs"][0]["message"] == "Really?"
    ask = _by_label(page.snap(), "Ask")[0]
    r = page.eval("window.__freyjaJev.act(%d, 'click', null, '', true)" % ask["id"])
    assert page.eval("document.title") == "confirmed"


def test_a_label_drawn_across_a_field_does_not_cover_it(page):
    """Outlined fields draw their label across the middle of the control, and
    the label is not inside it. The Google Cloud console's selects and an empty
    text field were refused as covered, so its form could not be filled."""
    hq = _by_label(page.snap(), "Headquarters")[0]
    assert hq["role"] == "combobox"
    assert page.act(hq["id"], "click", None, hq["guard"])["ok"] is True
    opts = [a for a in page.snap()["actions"] if a["role"] == "option"]
    assert [a["label"] for a in opts] == ["United States of America", "Canada", "Mexico"]
    assert page.act(opts[0]["id"], "click")["ok"] is True
    assert page.eval("document.getElementById('hq').textContent") == "United States of America"
    uc = _by_label(page.snap(), "Use cases")[0]
    r = page.act(uc["id"], "fill", {"text": "enterprise agents", "mode": "replace"}, uc["guard"])
    assert r == {"ok": True, "reason": None, "readback": "enterprise agents"}


def test_an_open_dropdown_still_covers_the_field_under_it(page):
    s = page.snap()
    hq, uc = _by_label(s, "Headquarters")[0], _by_label(s, "Use cases")[0]
    assert page.act(hq["id"], "click")["ok"] is True
    refused = page.act(uc["id"], "fill", {"text": "x", "mode": "replace"})
    assert refused["reason"].startswith("covered")
    assert page.act(0, "key", "Escape")["ok"] is True
    assert page.eval("!!document.getElementById('panel')") is False
    assert page.act(uc["id"], "fill", {"text": "x", "mode": "replace"})["ok"] is True


def test_enter_opens_a_focused_combobox(page):
    """Enter was refused outside a text field, so a focused dropdown could not
    be opened from the keyboard; Enter with nothing focused still is."""
    page.eval("document.getElementById('hq').focus()")
    assert page.act(0, "key", "Enter")["ok"] is True
    assert page.eval("document.getElementById('hq').getAttribute('aria-expanded')") == "true"
    page.act(0, "key", "Escape")
    page.eval("document.activeElement.blur()")
    assert page.act(0, "key", "Enter")["reason"] == "no_editable_focused"


# ─── page patterns from a real booking run (tests/fixtures/jev_dom/patterns.html) ───


def test_drawn_radios_are_listed_by_column_and_row_and_clicked_through_their_box(patterns):
    """A fare grid draws each choice as a box around a zero-size radio, and the
    radios' own labels repeat across columns. The run saw no fares at all."""
    s = patterns.snap()
    fares = [a for a in s["actions"] if a["role"] == "radio"]
    assert len(fares) == 6
    biz = [a for a in fares if a["label"].startswith("Business Bundle: $275 Standard")]
    assert len(biz) == 2 and "Biz fare" in biz[0]["label"]
    # Same label in two rows: each is told apart by its row, not the section heading.
    assert "9:05 PM" in biz[0]["context"] and "6:02 AM" in biz[1]["context"]
    eco = [a for a in fares if a["label"].startswith("Economy Bundle: $130 Standard")]
    assert eco and "6:02 AM" in eco[0]["context"]
    r = patterns.act(biz[0]["id"], "click", None, biz[0]["guard"])
    assert r["ok"] and patterns.eval("document.title") == "fare:z1"
    assert [a for a in patterns.snap()["actions"] if a["id"] == biz[0]["id"]][0]["checked"] is True


def test_an_icon_control_with_no_name_is_named_from_its_image(patterns):
    s = patterns.snap()
    search = [a for a in s["actions"] if a["label"] == "search"]
    assert search and "search_btn.svg" in search[0]["hint"]
    assert patterns.act(search[0]["id"], "click")["ok"]
    assert patterns.eval("document.title") == "searched"


def test_a_read_only_field_is_offered_as_a_click_with_its_value(patterns):
    who = [a for a in patterns.snap()["actions"] if a["label"] == "Travelers"][0]
    assert who["kind"] == "click" and who["value"] == "1 Adult"
    assert patterns.act(who["id"], "click")["ok"]
    assert patterns.eval("document.getElementById('who').value") == "panel open"


def test_suggestion_rows_without_roles_are_options_and_a_click_picks_one(patterns):
    """Airport suggestions were list items with no role and a tabindex=-1 wrapper:
    the run typed the city over and over and never committed it."""
    city = [a for a in patterns.snap()["actions"] if a["label"] == "City"][0]
    patterns.act(city["id"], "fill", {"text": "San", "mode": "replace"})
    rows = [a for a in patterns.snap()["actions"] if a["role"] == "option"]
    assert [a["label"] for a in rows] == [
        "San Francisco (SFO)", "San Jose (SJC)", "Santa Ana (SNA)"]
    assert patterns.act(rows[0]["id"], "click")["ok"]
    picked = patterns.eval("document.getElementById('cityPicked').textContent")
    assert picked == "picked:San Francisco (SFO)"


def test_down_then_enter_picks_a_suggestion_without_submitting(patterns):
    city = [a for a in patterns.snap()["actions"] if a["label"] == "City"][0]
    patterns.act(city["id"], "fill", {"text": "San J", "mode": "replace"})
    focused = [a for a in patterns.snap()["actions"] if a["focused"]][0]
    assert focused["enter"] == "pick"
    assert patterns.act(0, "key", "ArrowDown")["ok"]
    assert patterns.act(0, "key", "Enter")["ok"]
    picked = patterns.eval("document.getElementById('cityPicked').textContent")
    assert picked == "picked:San Jose (SJC)"


def test_a_click_on_a_plain_div_commits_the_field_typed_last(patterns):
    """A real press moves focus, so the field fires change; a synthetic click on a
    div did not, and a form that binds values on change never saw the last one."""
    s = patterns.snap()
    zip_ = [a for a in s["actions"] if a["label"] == "Zip code"][0]
    nxt = [a for a in s["actions"] if a["label"] == "Next step"][0]
    assert nxt["inferred"] is True
    patterns.act(zip_["id"], "fill", {"text": "80202", "mode": "replace"})
    assert patterns.act(nxt["id"], "click")["ok"]
    assert patterns.eval("document.title") == "next:model:80202"


def test_a_modal_layer_is_a_dialog_and_hides_what_is_behind_it(patterns):
    """An upsell with no dialog role was appended after a long page; its text was
    cut off and the run waited on a page it could not see was blocked."""
    btn = [a for a in patterns.snap()["actions"] if a["label"] == "Open offer"][0]
    patterns.act(btn["id"], "click")
    s = patterns.snap()
    assert s["dialog"]["title"] == "Join the club"
    assert s["text"].startswith("[dialog] Join the club")
    assert sorted(a["label"] for a in s["actions"]) == ["No thanks, continue", "SIGN UP"]
    assert s["behind"] > 5
    covered = [a for a in patterns.snap()["actions"] if a["label"] == "SIGN UP"]
    assert covered
    no = [a for a in s["actions"] if a["label"] == "No thanks, continue"][0]
    assert patterns.act(no["id"], "click")["ok"] and patterns.eval("document.title") == "declined"
    assert patterns.snap()["dialog"] is None


def test_a_covered_control_names_what_covers_it(patterns):
    s = patterns.snap()
    target = [a for a in s["actions"] if a["label"] == "Save slowly"][0]
    patterns.eval("document.getElementById('overlay').style.display='flex'")
    r = patterns.act(target["id"], "click", None, target["guard"])
    assert r["reason"].startswith("covered by div#overlay") and "Join the club" in r["reason"]


def test_frames_are_reported(patterns):
    s = patterns.snap()
    assert len(s["frames"]) == 1 and s["frames"][0]["title"] == "offers"


def test_requests_an_action_started_keep_the_page_busy(patterns):
    """A Continue that posts and navigates when the server answers looked
    settled after 3 s; the run judged it a no-op."""
    slow = [a for a in patterns.snap()["actions"] if a["label"] == "Save slowly"][0]
    assert patterns.act(slow["id"], "click")["ok"]
    q = json.loads(patterns.eval("window.__freyjaJev.quiet()"))
    assert q["p"] == 1
    time.sleep(1.5)
    q = json.loads(patterns.eval("window.__freyjaJev.quiet()"))
    assert q["p"] == 0 and patterns.eval("document.title") == "saved"


def test_a_panel_parked_beside_the_window_is_not_on_the_page(patterns):
    """Six closed slide-in panels each listed a "Close" button on every page of a
    real booking site; something in a side-scrolling carousel still counts."""
    labels = [a["label"] for a in patterns.snap()["actions"]]
    assert "Close" not in labels and "Book this deal" in labels


def test_typing_the_same_value_again_reopens_the_suggestions(patterns):
    """An autocomplete searches only when the value changes; a run retyped an
    airport code over itself and the suggestion list never came back."""
    city = [a for a in patterns.snap()["actions"] if a["label"] == "City"][0]
    patterns.act(city["id"], "fill", {"text": "San", "mode": "replace"})
    patterns.eval("document.getElementById('menu').style.display='none'")
    patterns.act(city["id"], "fill", {"text": "San", "mode": "replace"})
    assert patterns.eval("document.getElementById('menu').style.display") == "block"


def test_repeated_day_links_are_named_by_their_month_not_by_a_nav_link(patterns):
    """A real date picker's first line was its "Prev" link: day links read
    "29 (Prev)" and "29 (Next)", and the run picked the 31st, the one day without."""
    actions = patterns.snap()["actions"]
    days = [a for a in actions if a["label"] == "29"]
    assert [d["context"] for d in days] == ["October 2026", "November 2026"]
    # A day only one month offers (the 12th of October is past) names its month too.
    twelve = [a for a in actions if a["label"] == "12"]
    assert [d["context"] for d in twelve] == ["November 2026"]


def test_a_panel_that_only_its_opener_closes_is_closed_before_the_click(patterns):
    """A travelers panel ignored Escape and presses outside it; only its own
    field closed it. Three clicks on Search behind it were refused and the run
    stopped with the form filled in."""
    s = patterns.snap()
    guests = [a for a in s["actions"] if a["label"] == "Guests"][0]
    patterns.act(guests["id"], "click")
    go = [a for a in patterns.snap()["actions"] if a["label"] == "Go"][0]
    r = patterns.act(go["id"], "click")
    assert r["ok"] and "Guests" in r["closedFirst"]
    assert patterns.eval("document.title") == "go"
    assert patterns.eval("document.getElementById('pax').getAttribute('aria-expanded')") == "false"


def test_a_button_on_a_table_row_is_named_by_the_whole_row(lists):
    """Five "Suspend" buttons read "Ada Carter": a row was named by its first
    cell, and the email that tells two members of one name apart was in the next."""
    sus = [a["context"] for a in lists.snap()["actions"] if a["label"] == "Suspend"]
    assert sus[0] == "Grace Hopper · grace.hopper.1347@example.com · Member · Active"
    assert "grace.hopper.1437@example.com" in sus[1] and "grace.hooper.1437@example.com" in sus[2]
    assert len(set(sus)) == 4 and not any("Suspend" in c for c in sus)
    # A row no other row resembles is read across too.
    assert sus[3] == "Ada Carter · ada.carter@example.com · Admin · Active"


def test_look_alike_rows_without_table_roles_get_all_their_text(lists):
    """Grace Hooper beside Grace Hopper: two names a letter apart read alike, so
    each row's button carries its whole row, email and all."""
    edit = [a["context"] for a in lists.snap()["actions"] if a["label"] == "Edit"]
    assert edit[0] == "Alan Turing · alan.turing.1@example.com"
    assert edit[1] == "Alan Turing · alan.turing.2@example.com"
    assert edit[2] == "Alan Tuning · alan.tuning@example.com"
    assert edit[3] == "Barbara Liskov", "a name no other row resembles stays short"
    names = [a["context"] for a in lists.snap()["actions"] if a["label"] == "First name"]
    assert names == ["Passenger 1", "Passenger 2"]


def test_return_in_a_search_box_with_suggestions_searches(lists):
    """Told that a field with open suggestions commits nothing until one is
    picked, a catalog run pressed Down and Return in the search box and opened
    the first model instead of searching."""
    def enter(field_id, label):
        lists.eval(f"document.getElementById('{field_id}').focus()")
        return [a for a in lists.snap()["actions"] if a["label"] == label][0]["enter"]

    assert enter("q", "Search models") == "search-pick"
    assert enter("from", "From airport") == "pick"


ROOMS = ROOT / "tests/fixtures/jev_dom/rooms.html"


@pytest.fixture()
def rooms():
    with _open(ROOMS) as p:
        yield p


def test_short_controls_are_named_by_their_card(rooms):
    # Two room cards each had "VIEW DETAILS"; once one read "VIEWING", neither
    # label repeated, both lost their room, and a run opened the wrong room.
    s = rooms.snap()
    assert _by_label(s, "VIEW DETAILS")[0]["context"] == "Deluxe Room 1 King Bed"
    assert _by_label(s, "VIEWING")[0]["context"] == "Deluxe Pet-Friendly Room 1 King Bed"


def test_collapsed_and_faded_text_is_not_page_text(rooms):
    # Both rooms' detail panels stayed in the page, and the closed one's rate
    # read as the open room's.
    s = rooms.snap()
    assert "OPEN-PET-PANEL" in s["text"] and "SHOWN-SLIDE" in s["text"]
    assert "HIDDEN-DELUXE-PANEL" not in s["text"] and "FADED-SLIDE" not in s["text"]


def test_a_long_page_is_read_whole(rooms):
    # At 6000 characters a policy below the room cards was cut mid-word.
    s = rooms.snap()
    assert "POLICY-END" in s["text"] and s["textCut"] is False
    assert s["text"].index("[below the visible area]") < s["text"].index("POLICY-END")


def test_text_above_the_view_is_returned_apart(rooms):
    rooms.eval("window.scrollTo(0, 1500)")
    s = rooms.snap()
    assert "Rooms and rates" in s["above"] and "Rooms and rates" not in s["text"]
    rooms.eval("window.scrollTo(0, 0)")


CALENDAR = ROOT / "tests/fixtures/jev_dom/calendar.html"


@pytest.fixture()
def calendar():
    with _open(CALENDAR) as p:
        yield p


def test_days_only_one_month_has_still_name_their_month(calendar):
    # January's 29, 30 and 31 read without a month beside "28 (January)", because
    # no February day shares their label and the header had no year in it.
    s = calendar.snap()
    assert [a["context"] for a in _by_label(s, "28")] == ["January", "February"]
    for d in ("29", "30", "31"):
        assert [a["context"] for a in _by_label(s, d)] == ["January"], d
