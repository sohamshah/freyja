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


def _tool(monkeypatch, cls, default_display_id=None):
    async def no_frame(spec, native, **kw):
        return None

    monkeypatch.setattr(ct, "_require_screen_recording", lambda call_id, tool: None)
    monkeypatch.setattr(ct, "_import_native", lambda: _Desk)
    monkeypatch.setattr(ct, "_emit_frame", no_frame)
    spec = ct.ComputerToolSpec(
        session_id="t", emit_event=lambda e: None, cancel_event=asyncio.Event(),
        default_display_id=default_display_id,
    )
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
