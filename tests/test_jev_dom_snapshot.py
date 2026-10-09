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


@pytest.fixture(scope="module")
def page():
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
         FIXTURE.as_uri()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2))
                target = next((t for t in tabs if t.get("type") == "page" and "basic.html" in t.get("url", "")), None)
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
    assert page.act(cov["id"], "click", None, cov["guard"])["reason"] == "covered"
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
    assert page.act(uc["id"], "fill", {"text": "x", "mode": "replace"})["reason"] == "covered"
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
