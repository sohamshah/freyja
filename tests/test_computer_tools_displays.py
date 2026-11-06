"""Multi-monitor coordinate mapping in bridge/tools/computer_tools.py.

Input events use one global space across displays (primary top-left at
(0,0), others at offsets, often negative); a screenshot of display N is
local to N. The spec's native_origin carries N's corner so api <-> native
translation lands on the right monitor. A display id that is not connected
is an error that names the displays that are. No screen is touched.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from bridge.tools import computer_tools as ct


def _spec(origin=(0, 0)):
    spec = ct.ComputerToolSpec(session_id="t", emit_event=lambda e: None, cancel_event=asyncio.Event())
    spec.native_dims = (1920, 1080)
    spec.api_dims = (1280, 720)
    spec.native_origin = origin
    return spec


def test_primary_display_mapping_is_unchanged():
    spec = _spec()
    assert ct._api_to_native(spec, 640, 360) == (960, 540)
    assert ct._native_to_api(spec, 960, 540) == (640, 360)


def test_display_above_right_adds_its_origin():
    spec = _spec(origin=(923, -1080))
    assert ct._api_to_native(spec, 640, 360) == (1883, -540)
    assert ct._native_to_api(spec, 1883, -540) == (640, 360)
    # a point on another monitor comes back outside this frame, not clamped
    assert ct._native_to_api(spec, 100, 100) == (-549, 787)


def test_ax_tree_bounds_translate_relative_to_the_display():
    spec = _spec(origin=(-997, -1080))
    tree = {"role": "AXWindow", "bounds": [-997, -1080, 1920, 1080], "children": [{"bounds": [-37, -540, 30, 15]}]}
    ct._translate_ax_tree_bounds(spec, tree)
    assert tree["bounds"] == [0, 0, 1280, 720]
    assert tree["children"][0]["bounds"] == [640, 360, 20, 10]


def test_display_geometry_is_empty_off_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert ct.display_geometry() == {}
    assert ct._display_origin(2) == (0, 0)


class _Desk:
    """A fake freyja_native with displays 1 (primary) and 2 whose captures
    all fail, as both capture paths do for a display that is not connected.
    Nothing is captured."""

    @staticmethod
    def list_displays():
        return [
            SimpleNamespace(id=1, width=3024, height=1964, scale=2.0, is_primary=True),
            SimpleNamespace(id=2, width=2560, height=1440, scale=1.0, is_primary=False),
        ]

    @staticmethod
    def screenshot(**kw):
        raise RuntimeError(f"capture proxy 404: display {kw['display_id']} is not connected")


class _Hands(_Desk):
    """_Desk that writes down the input a tool asks for. Nothing is sent."""

    def __init__(self):
        self.sent = []

    def click(self, x, y, **kw):
        self.sent.append(("click", x, y))

    def move_mouse(self, x, y):
        self.sent.append(("move_mouse", x, y))

    def scroll(self, dx, dy, *, x=None, y=None):
        self.sent.append(("scroll", x, y))


def _tool(monkeypatch, cls, default_display_id=None, *, native=_Desk, framed=True):
    """`framed` means a frame was already taken, so the spec has its dims."""

    async def no_frame(spec, native, **kw):
        return None

    async def no_highlight(spec, ms=0):
        return True

    monkeypatch.setattr(ct, "_require_screen_recording", lambda call_id, tool: None)
    monkeypatch.setattr(ct, "_require_accessibility", lambda call_id, tool: None)
    monkeypatch.setattr(ct, "_await_highlight_or_cancel", no_highlight)
    monkeypatch.setattr(ct, "_import_native", lambda: native)
    monkeypatch.setattr(ct, "_emit_frame", no_frame)
    spec = ct.ComputerToolSpec(
        session_id="t", emit_event=lambda e: None, cancel_event=asyncio.Event(),
        default_display_id=default_display_id,
    )
    if framed:
        spec.native_dims, spec.api_dims = (1920, 1080), (1280, 720)
    return cls(spec)


async def test_screenshot_of_a_display_that_is_not_connected_names_the_ones_that_are(monkeypatch):
    res = await _tool(monkeypatch, ct.ScreenshotTool).execute("c1", {"display_id": 99})
    assert res.is_error
    assert res.content == (
        "screenshot: display 99 is not connected (it may have been unplugged, or the id is "
        "from an old list). Connected displays: 1 (primary), 2. Pass one of those ids as "
        "display_id, or leave display_id out for the main display."
    )


async def test_a_pinned_display_that_is_gone_says_the_session_is_pinned(monkeypatch):
    res = await _tool(monkeypatch, ct.ScreenshotTool, default_display_id=7).execute("c1", {})
    assert res.is_error
    assert res.content.startswith(
        "screenshot: this session is pinned to display 7, which is not connected"
    )
    assert res.content.endswith(
        "Connected displays: 1 (primary), 2. Pass one of those ids as display_id."
    )


async def test_a_connected_display_that_fails_keeps_the_permission_hint(monkeypatch):
    res = await _tool(monkeypatch, ct.ScreenshotTool).execute("c1", {"display_id": 2})
    assert res.is_error and "Screen Recording" in res.content


async def test_inspect_region_of_a_display_that_is_not_connected(monkeypatch):
    tool = _tool(monkeypatch, ct.InspectRegionTool)
    res = await tool.execute("c1", {"x1": 0, "y1": 0, "x2": 10, "y2": 10, "display_id": 99})
    assert res.is_error
    assert res.content.startswith("inspect_region: display 99 is not connected")


GONE_PIN = (
    ": this session is pinned to display 7, which is not connected (it may have been "
    "unplugged, or the id is from an old list). Connected displays: 1 (primary), 2. "
    "Take a screenshot with one of those ids as display_id first; coordinates then "
    "refer to that screenshot."
)


@pytest.mark.parametrize(
    ("cls", "name", "args"),
    [
        (ct.ClickTool, "click", {"x": 640, "y": 360}),
        (ct.MoveMouseTool, "move_mouse", {"x": 640, "y": 360}),
        (ct.ScrollTool, "scroll", {"x": 640, "y": 360, "dy": 3}),
        (ct.ListWindowsTool, "list_windows", {}),
        (ct.FindElementTool, "find_element", {"pid": 1, "label": "OK"}),
        (ct.ReadAxTreeTool, "read_ax_tree", {"pid": 1}),
        (ct.CursorPositionTool, "cursor_position", {}),
        (ct.InspectRegionTool, "inspect_region", {"x1": 0, "y1": 0, "x2": 10, "y2": 10}),
    ],
)
async def test_a_pinned_display_that_is_gone_is_not_swapped_for_the_main_one(monkeypatch, cls, name, args):
    # No frame yet, so the tool sizes the coordinate space from the pinned
    # display. The main display's size and corner would put the click on
    # the wrong screen.
    hands = _Hands()
    tool = _tool(monkeypatch, cls, default_display_id=7, native=hands, framed=False)
    res = await tool.execute("c1", {**args, "settle_ms": 0})
    assert res.is_error
    assert res.content == name + GONE_PIN
    assert hands.sent == []
    assert tool._spec.native_dims is None


async def test_a_pinned_display_with_no_frame_yet_maps_onto_itself(monkeypatch):
    monkeypatch.setattr(ct, "display_geometry", lambda: {2: ct.DisplayGeometry(-2560, 0, 2560, 1440, False)})
    hands = _Hands()
    tool = _tool(monkeypatch, ct.ClickTool, default_display_id=2, native=hands, framed=False)
    res = await tool.execute("c1", {"x": 640, "y": 360, "settle_ms": 0})
    assert not res.is_error
    assert hands.sent == [("click", -1280, 720)]


async def test_scroll_at_the_cursor_needs_no_display(monkeypatch):
    hands = _Hands()
    tool = _tool(monkeypatch, ct.ScrollTool, default_display_id=7, native=hands, framed=False)
    res = await tool.execute("c1", {"dy": 3, "settle_ms": 0})
    assert not res.is_error
    assert hands.sent == [("scroll", None, None)]
