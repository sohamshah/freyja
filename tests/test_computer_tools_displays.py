"""Multi-monitor coordinate mapping in bridge/tools/computer_tools.py.

Input events use one global space across displays (primary top-left at
(0,0), others at offsets, often negative); a screenshot of display N is
local to N. The spec's native_origin carries N's corner so api <-> native
translation lands on the right monitor. Pure math — no screen touched.
"""

from __future__ import annotations

import asyncio
import sys

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
