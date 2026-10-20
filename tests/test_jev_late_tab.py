"""A tab a click opens after the click returned.

A file viewer's Download asked its server first and opened the virus-scan
warning in a new tab about a second later. The page script reports a new tab
only when the click itself opens one, so the run went on reading the viewer: the
warning's "Download anyway" was never listed, and the run waited until it was
stopped.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from bridge.tools.jev_operator import dom_surface
from bridge.tools.jev_operator.dom_surface import LATE_TAB_S, DOMSurface
from tests.test_jev_dom_surface import TARGET, FakePage, action, run, snap

VIEWER = "https://files.example.com/file/d/7/view"
WARNING = "https://files.usercontent.example.com/download?id=7&export=download"


class Viewer(FakePage):
    def __call__(self, bundle, js):
        if js.endswith("snapshot();})()"):
            return json.dumps(snap(self.actions, url=VIEWER))
        return super().__call__(bundle, js)


def _surface(monkeypatch, front: dict, tabs: dict):
    page = Viewer([action(1, label="Download")])
    log: list[dict] = []
    s = DOMSurface(bundle=TARGET.bundle, actuator=SimpleNamespace(), run_js=page,
                   js_source="/*js*/", log=log.append)
    s._injected_js = False  # as in a browser: tabs are read through AppleScript
    s.tab_id, s.known_tabs, s.url = "A", {"A", "B"}, VIEWER
    asked: list[str] = []

    def info(bundle, tid, timeout_s=None):
        asked.append(tid)
        return "", tabs.get(tid, "")

    monkeypatch.setattr(dom_surface, "_RUN_TABS", {})
    monkeypatch.setattr(dom_surface, "active_tab_id", lambda b, timeout_s=None: front["id"])
    monkeypatch.setattr(dom_surface, "tab_info", info)
    monkeypatch.setattr(DOMSurface, "settle", lambda self, kind, min_s=0.0: asyncio.sleep(0))
    return s, log, asked


def test_a_tab_the_click_opens_late_is_followed_at_the_next_read(monkeypatch):
    front = {"id": "A"}
    s, log, _ = _surface(monkeypatch, front, {"C": WARNING})
    obs = run(s.observe(TARGET))
    assert run(s.execute("click", obs.elements[0])).ok
    run(s.observe(TARGET))
    assert s.tab_id == "A", "no new tab yet: the run stays on the viewer"
    front["id"] = "C"  # the warning opens a second later
    run(s.observe(TARGET))
    assert s.tab_id == "C" and s.own_tab and dom_surface.run_tab(TARGET.bundle, "C")
    assert log[-1] == {"event": "dom_tab", "tab": "C",
                       "how": "the click opened a new tab after it returned"}


def test_a_late_tab_on_another_site_or_one_the_person_had_is_not_followed(monkeypatch):
    front = {"id": "D"}
    s, log, asked = _surface(monkeypatch, front, {"D": "https://mail.other.example/inbox"})
    s._clicked_at = time.monotonic()
    run(s._follow_late_tab())
    run(s._follow_late_tab())
    assert s.tab_id == "A" and asked == ["D"], "a tab passed over is not looked up again"
    assert log[0]["event"] == "dom_tab_not_followed" and "another site" in log[0]["why"]
    front["id"] = "B"  # a tab that existed when the run started
    run(s._follow_late_tab())
    assert s.tab_id == "A"


def test_the_front_tab_is_checked_only_for_a_while_after_a_click(monkeypatch):
    front = {"id": "C"}
    s, _, asked = _surface(monkeypatch, front, {"C": WARNING})
    s._clicked_at = time.monotonic() - LATE_TAB_S - 1
    run(s._follow_late_tab())
    assert s.tab_id == "A" and s._clicked_at is None and asked == []
