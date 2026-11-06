"""Multi-monitor coordinate mapping in bridge/tools/computer_tools.py.

Input events use one global space across displays (primary top-left at
(0,0), others at offsets, often negative); a screenshot of display N is
local to N. The spec's native_origin carries N's corner so api <-> native
translation lands on the right monitor. A display id that is not connected
is an error that names the displays that are. The screenshot after an action,
and the frame-dump trim, are checked here too, and what focus_window says when
only the app came forward. No screen is touched, and the freyja_native wrapper
is loaded over a stub, never the built extension.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from pathlib import Path
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


async def test_find_element_on_an_app_that_does_not_answer_times_out(monkeypatch):
    # The search ran with no time limit; an app that answered slowly held the
    # agent's tool call for as long as the walk took.
    import threading

    release = threading.Event()

    class Stalled(_Desk):
        def find_ax_element(self, pid, **kw):
            release.wait(30)
            return None

    monkeypatch.setattr(ct, "AX_TOOL_TIMEOUT_S", 0.1)
    tool = _tool(monkeypatch, ct.FindElementTool, native=Stalled())
    try:
        res = await asyncio.wait_for(tool.execute("c1", {"pid": 1, "label": "OK"}), 10.0)
    finally:
        release.set()  # the loop joins the abandoned thread when it closes
    assert res.is_error
    assert res.content.startswith("find_element timed out")


class _Monitors(_Hands):
    """Displays 1 (primary) and 2 (left of it) that a test can unplug, move or
    resize. A capture returns a stand-in frame of the display's preview size
    and notes which display it was asked for. Nothing is captured or sent."""

    def __init__(self, *, only_primary=False):
        super().__init__()
        self.displays = {d.id: d for d in _Desk.list_displays() if d.is_primary or not only_primary}
        self.geometry = {
            1: ct.DisplayGeometry(0, 0, 3024, 1964, True),
            2: ct.DisplayGeometry(-2560, 0, 2560, 1440, False),
        }
        if only_primary:
            del self.geometry[2]
        self.shots = []
        self.cursor = (0, 0)
        self.windows = []
        # What focus_window says: True when the window itself was raised,
        # False when only its app came forward, None from a native module
        # built before it said.
        self.raised = None

    def list_displays(self):
        return list(self.displays.values())

    def screenshot(self, *, display_id=None, window_id=None, max_dim=None, format="jpeg", quality=75):
        self.shots.append(display_id)
        d = self.displays.get(1 if display_id is None else display_id)
        if d is None:
            raise RuntimeError(f"capture proxy 404: display {display_id} is not connected")
        w, h = ct._compute_api_dims(d.width, d.height)
        return SimpleNamespace(png=b"frame", width=w, height=h, format="jpeg", mime_type="image/jpeg")

    def cursor_position(self):
        return self.cursor

    def move_mouse(self, x, y):
        super().move_mouse(x, y)
        self.cursor = (x, y)

    def type_text(self, text):
        self.sent.append(("type_text", text))

    def press_key(self, key, modifiers=None):
        self.sent.append(("press_key", key))

    def key_up(self, key):
        self.sent.append(("key_up", key))

    def focus_app(self, bundle_id):
        self.sent.append(("focus_app", bundle_id))

    def focus_window(self, window_id):
        self.sent.append(("focus_window", window_id))
        return self.raised

    def list_windows(self, include_helpers=False):
        return list(self.windows)


def _session(monkeypatch, screens, default_display_id=None):
    """Every atomic tool on one spec, over `screens`. Frames are not written
    to disk and the display layout is the fake's."""

    async def no_highlight(spec, ms=0):
        return True

    monkeypatch.setattr(ct, "_require_screen_recording", lambda call_id, tool: None)
    monkeypatch.setattr(ct, "_require_accessibility", lambda call_id, tool: None)
    monkeypatch.setattr(ct, "_await_highlight_or_cancel", no_highlight)
    monkeypatch.setattr(ct, "_import_native", lambda: screens)
    monkeypatch.setattr(ct, "_FRAME_DUMP_ENABLED", False)
    monkeypatch.setattr(ct, "display_geometry", lambda: dict(screens.geometry))
    spec = ct.ComputerToolSpec(
        session_id="t", emit_event=lambda e: None, cancel_event=asyncio.Event(),
        default_display_id=default_display_id,
    )
    return {t.definition.name: t for t in ct.build_computer_tools(spec)}, spec


def _text(res):
    return res.content if isinstance(res.content, str) else res.content[0].text


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("click", {"x": 640, "y": 360}),
        ("scroll", {"x": 640, "y": 360, "dy": 3}),
        ("type_text", {"text": "hi"}),
        ("press_key", {"key": "return"}),
        ("key_up", {"key": "cmd"}),
        ("focus_window", {"bundle_id": "com.apple.finder"}),
    ],
)
@pytest.mark.parametrize("pinned", [None, 1])
async def test_the_screenshot_after_an_action_is_of_the_display_last_looked_at(monkeypatch, name, args, pinned):
    # The model looked at display 2, so its coordinates are display 2's. The
    # screenshot after the action must show display 2 too, and say so, or the
    # model reads the main display as the place it just acted on.
    screens = _Monitors()
    tools, spec = _session(monkeypatch, screens, default_display_id=pinned)
    await tools["screenshot"].execute("c1", {"display_id": 2})
    res = await tools[name].execute("c2", {**args, "settle_ms": 0})
    assert not res.is_error
    assert screens.shots == [2, 2]
    assert (spec.api_dims, spec.native_origin) == ((1280, 720), (-2560, 0))
    assert _text(res).endswith(" The screenshot is of display 2 (1280x720); your next x,y refer to it.")


async def test_clicks_after_looking_at_another_display_stay_on_it(monkeypatch):
    screens = _Monitors()
    tools, _ = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {"display_id": 2})
    for call in ("c2", "c3"):
        await tools["click"].execute(call, {"x": 640, "y": 360, "settle_ms": 0})
    assert screens.sent == [("click", -1280, 720), ("click", -1280, 720)]


async def test_one_display_keeps_the_plain_action_summary(monkeypatch):
    screens = _Monitors(only_primary=True)
    tools, _ = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {})
    res = await tools["click"].execute("c2", {"x": 640, "y": 360, "settle_ms": 0})
    assert _text(res) == "Clicked at (640, 360) with left."
    assert screens.shots == [None, 1]


STALE_ARGS = [
    ("click", {"x": 640, "y": 360}),
    ("move_mouse", {"x": 640, "y": 360}),
    ("scroll", {"x": 640, "y": 360, "dy": 3}),
    ("cursor_position", {}),
    ("list_windows", {}),
]


@pytest.mark.parametrize(("name", "args"), STALE_ARGS)
async def test_a_pinned_display_unplugged_after_its_screenshot_fails_instead_of_using_its_old_place(
    monkeypatch, name, args
):
    screens = _Monitors()
    tools, spec = _session(monkeypatch, screens, default_display_id=2)
    await tools["screenshot"].execute("c1", {})
    del screens.displays[2], screens.geometry[2]
    res = await tools[name].execute("c2", {**args, "settle_ms": 0})
    assert res.is_error
    assert res.content == (
        f"{name}: this session is pinned to display 2, which is not connected (it may have been "
        "unplugged, or the id is from an old list). Connected displays: 1 (primary). Take a "
        "screenshot with one of those ids as display_id first; coordinates then refer to that screenshot."
    )
    assert screens.sent == []


async def test_a_display_looked_at_and_then_unplugged_fails_the_click(monkeypatch):
    screens = _Monitors()
    tools, _ = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {"display_id": 2})
    del screens.displays[2], screens.geometry[2]
    res = await tools["click"].execute("c2", {"x": 640, "y": 360, "settle_ms": 0})
    assert res.is_error
    assert res.content.startswith(
        "click: display 2, the one your coordinates refer to, is not connected (it may have been unplugged"
    )
    assert screens.sent == []
    # a new screenshot of the main display makes its coordinates usable again
    await tools["screenshot"].execute("c3", {})
    res = await tools["click"].execute("c4", {"x": 640, "y": 360, "settle_ms": 0})
    assert not res.is_error
    assert screens.sent == [("click", 1512, 851)]


async def test_a_display_moved_in_the_arrangement_takes_the_click_to_its_new_place(monkeypatch):
    # The screenshot is local to the display, so it still matches the screen;
    # only the display's corner in the global layout changed.
    screens = _Monitors()
    tools, spec = _session(monkeypatch, screens, default_display_id=2)
    await tools["screenshot"].execute("c1", {})
    screens.geometry[2] = ct.DisplayGeometry(3024, -200, 2560, 1440, False)
    res = await tools["click"].execute("c2", {"x": 640, "y": 360, "settle_ms": 0})
    assert not res.is_error
    assert screens.sent == [("click", 4304, 520)]
    assert spec.native_origin == (3024, -200)
    # with no layout to read, the saved corner stays
    screens.geometry.clear()
    await tools["click"].execute("c3", {"x": 640, "y": 360, "settle_ms": 0})
    assert screens.sent[-1] == ("click", 4304, 520)


@pytest.mark.parametrize(("name", "args"), STALE_ARGS)
async def test_a_display_that_changed_resolution_asks_for_a_new_screenshot(monkeypatch, name, args):
    screens = _Monitors()
    tools, _ = _session(monkeypatch, screens, default_display_id=2)
    await tools["screenshot"].execute("c1", {})
    screens.displays[2] = SimpleNamespace(id=2, width=1920, height=1080, scale=1.0, is_primary=False)
    screens.geometry[2] = ct.DisplayGeometry(-1920, 0, 1920, 1080, False)
    res = await tools[name].execute("c2", {**args, "settle_ms": 0})
    assert res.is_error
    assert res.content == (
        f"{name}: display 2 changed size from 2560x1440 to 1920x1080 since your last screenshot "
        "of it, so your coordinates no longer match the screen. Take a new screenshot first; "
        "coordinates then refer to that screenshot."
    )
    assert screens.sent == []
    await tools["screenshot"].execute("c3", {})
    res = await tools["click"].execute("c4", {"x": 640, "y": 360, "settle_ms": 0})
    assert not res.is_error
    assert screens.sent == [("click", -960, 540)]


async def test_a_window_screenshot_says_clicks_still_use_the_display_screenshot(monkeypatch):
    # A window capture does not set the coordinate space: clicks keep mapping
    # through the last display screenshot. The result must not tell the model
    # to click by the window picture's pixels.
    screens = _Monitors()
    tools, _ = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {"display_id": 2})
    res = await tools["screenshot"].execute("c2", {"window_id": 5})
    assert not res.is_error
    assert _text(res).startswith("Captured window 5 (1280x831, 0KB). This picture is for reading only:")
    assert "your last display screenshot (display 2, 1280x720)" in _text(res)
    assert "ONE coordinate space" not in _text(res)
    await tools["click"].execute("c3", {"x": 640, "y": 360, "settle_ms": 0})
    assert screens.sent == [("click", -1280, 720)]


@pytest.mark.parametrize("other", ["jev_x_8", "jev_x_12"])
async def test_the_frame_trim_keeps_to_the_session_that_wrote_the_frame(monkeypatch, tmp_path, other):
    # An id can be another id plus "_..." ("jev_x" and "jev_x_8"). Matching
    # "jev_x_*" counted the other session's frames as jev_x's, so the trim
    # removed jev_x's new frame (for jev_x_8) or one of jev_x_12's frames.
    monkeypatch.setattr(ct, "_FRAME_DUMP_ENABLED", True)
    monkeypatch.setattr(ct, "_FRAME_DUMP_DIR", str(tmp_path))
    # one frame per millisecond, so no two frames share a name
    ticks = iter(range(1779000000000, 1779000001000))
    monkeypatch.setattr(ct, "time", SimpleNamespace(time=lambda: next(ticks) / 1000))
    theirs = [f"{other}_{1778000000000 + i}_post_click.jpg" for i in range(20)]
    for name in theirs:
        (tmp_path / name).write_bytes(b"theirs")
    spec = ct.ComputerToolSpec(session_id="jev_x", emit_event=lambda e: None, cancel_event=asyncio.Event())
    for _ in range(22):
        assert await ct._emit_frame(spec, _Monitors(only_primary=True), reason="screenshot_tool")
    names = {p.name for p in tmp_path.iterdir()}
    assert set(theirs) <= names
    mine = [n for n in names if re.fullmatch(r"jev_x_\d+_screenshot_tool\.jpg", n)]
    assert len(mine) == 20
    assert names == set(theirs) | set(mine) | {"latest.jpg"}


NO_SHOT = " The action was done, but no screenshot could be taken after it: "
SHOT_FIRST = (
    "Take a screenshot with one of those ids as display_id first; coordinates then refer to "
    "that screenshot."
)


async def test_a_display_unplugged_during_a_click_says_the_click_was_done_and_why_no_screenshot(monkeypatch):
    screens = _Monitors()
    tools, _ = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {"display_id": 2})

    def click_then_unplug(x, y, **kw):
        screens.sent.append(("click", x, y))
        del screens.displays[2], screens.geometry[2]

    screens.click = click_then_unplug
    res = await tools["click"].execute("c2", {"x": 640, "y": 360, "settle_ms": 0})
    assert not res.is_error
    assert screens.sent == [("click", -1280, 720)]
    assert res.content == (
        "Clicked at (640, 360) with left." + NO_SHOT + "display 2, the one your coordinates refer "
        "to, is not connected (it may have been unplugged, or the id is from an old list). "
        "Connected displays: 1 (primary). " + SHOT_FIRST
    )


@pytest.mark.parametrize(
    ("name", "args", "summary", "sent"),
    [
        ("press_key", {"key": "return"}, "Pressed return.", ("press_key", "return")),
        ("type_text", {"text": "hi"}, "Typed 2 characters.", ("type_text", "hi")),
    ],
)
async def test_a_pinned_display_unplugged_before_a_key_action_says_why_there_is_no_screenshot(
    monkeypatch, name, args, summary, sent
):
    # Keys need no coordinates, so the action runs; only its screenshot fails.
    screens = _Monitors()
    tools, _ = _session(monkeypatch, screens, default_display_id=2)
    await tools["screenshot"].execute("c1", {})
    del screens.displays[2], screens.geometry[2]
    res = await tools[name].execute("c2", {**args, "settle_ms": 0})
    assert not res.is_error
    assert screens.sent == [sent]
    assert res.content == (
        summary + NO_SHOT + "this session is pinned to display 2, which is not connected (it may "
        "have been unplugged, or the id is from an old list). Connected displays: 1 (primary). "
        + SHOT_FIRST
    )


async def test_a_connected_display_whose_screenshot_fails_after_an_action_keeps_the_permission_hint(monkeypatch):
    screens = _Monitors()
    tools, _ = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {"display_id": 2})

    def refuse(**kw):
        raise RuntimeError("CGDisplayCreateImage returned null")

    screens.screenshot = refuse
    res = await tools["press_key"].execute("c2", {"key": "return", "settle_ms": 0})
    assert not res.is_error
    assert res.content == (
        "Pressed return." + NO_SHOT + "capture failed. Likely missing Screen Recording permission "
        "— open System Settings → Privacy & Security → Screen Recording and enable it for this app."
    )


def _window(wid, bundle, x, y, w=800, h=600):
    """A window as list_windows reports it: global bounds in points."""
    return SimpleNamespace(
        id=wid, pid=100 + wid, bundle=bundle, title="", is_frontmost=False, layer=0,
        bounds=SimpleNamespace(x=x, y=y, w=w, h=h),
    )


@pytest.mark.parametrize(
    ("args", "label"),
    [({"window_id": 5}, "window #5"), ({"bundle_id": "com.apple.notes"}, "com.apple.notes")],
)
async def test_focus_window_shows_the_display_that_holds_the_window(monkeypatch, args, label):
    # The model looked at display 1, and the window it focuses is on display 2.
    # It wants to see that window, so the screenshot, and the coordinate space
    # with it, move to display 2.
    screens = _Monitors()
    screens.windows = [_window(4, "com.apple.finder", 100, 100), _window(5, "com.apple.Notes", -2000, 200)]
    tools, spec = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {})
    res = await tools["focus_window"].execute("c2", {**args, "settle_ms": 0})
    assert not res.is_error
    assert screens.shots == [None, 2]
    assert (spec.frame_display_id, spec.api_dims, spec.native_origin) == (2, (1280, 720), (-2560, 0))
    assert _text(res) == (
        f"Focused {label}, which is on display 2. "
        "The screenshot is of display 2 (1280x720); your next x,y refer to it."
    )
    await tools["click"].execute("c3", {"x": 640, "y": 360, "settle_ms": 0})
    assert screens.sent[-1] == ("click", -1280, 720)


@pytest.mark.parametrize(
    ("args", "label"),
    [({"window_id": 5}, "window #5"), ({"bundle_id": "com.apple.notes"}, "com.apple.notes")],
)
@pytest.mark.parametrize("looked_at", [None, 2])
async def test_focus_window_in_a_pinned_session_stays_on_the_pinned_display(monkeypatch, args, label, looked_at):
    # The session is pinned to display 1, and its sub-agent is told not to
    # touch other monitors. The window it focuses is on display 2. The
    # screenshot and the coordinate space stay on display 1, also when the
    # model had looked at display 2, and the result says where the window is.
    screens = _Monitors()
    screens.windows = [_window(4, "com.apple.finder", 100, 100), _window(5, "com.apple.Notes", -2000, 200)]
    tools, spec = _session(monkeypatch, screens, default_display_id=1)
    await tools["screenshot"].execute("c1", {} if looked_at is None else {"display_id": looked_at})
    res = await tools["focus_window"].execute("c2", {**args, "settle_ms": 0})
    assert not res.is_error
    assert screens.shots == [looked_at or 1, 1]
    assert (spec.frame_display_id, spec.api_dims, spec.native_origin) == (1, (1280, 831), (0, 0))
    assert _text(res) == (
        f"Focused {label}, which is on display 2, outside this session's display (display 1). "
        "The screenshot is of display 1 (1280x831); your next x,y refer to it."
    )
    await tools["click"].execute("c3", {"x": 640, "y": 360, "settle_ms": 0})
    assert screens.sent[-1] == ("click", 1512, 851)


async def test_focus_window_in_a_pinned_session_shows_the_pinned_display_that_holds_the_window(monkeypatch):
    # The model looked at display 2; the window is on display 1, the pinned one.
    screens = _Monitors()
    screens.windows = [_window(5, "com.apple.Notes", 100, 100)]
    tools, spec = _session(monkeypatch, screens, default_display_id=1)
    await tools["screenshot"].execute("c1", {"display_id": 2})
    res = await tools["focus_window"].execute("c2", {"window_id": 5, "settle_ms": 0})
    assert screens.shots == [2, 1]
    assert spec.frame_display_id == 1
    assert _text(res) == (
        "Focused window #5, which is on display 1. "
        "The screenshot is of display 1 (1280x831); your next x,y refer to it."
    )


async def test_focus_window_on_a_window_across_two_displays_shows_the_one_with_most_of_it(monkeypatch):
    screens = _Monitors()
    screens.windows = [_window(5, "com.apple.Notes", -300, 100)]  # 300 wide on 2, 500 on 1
    tools, spec = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {"display_id": 2})
    res = await tools["focus_window"].execute("c2", {"window_id": 5, "settle_ms": 0})
    assert screens.shots == [2, 1]
    assert spec.frame_display_id == 1
    assert _text(res).startswith("Focused window #5, which is on display 1. The screenshot is of display 1")


async def test_focus_window_with_one_display_keeps_the_plain_summary(monkeypatch):
    screens = _Monitors(only_primary=True)
    screens.windows = [_window(5, "com.apple.Notes", 100, 100)]
    tools, _ = _session(monkeypatch, screens)
    res = await tools["focus_window"].execute("c1", {"window_id": 5, "settle_ms": 0})
    assert _text(res) == "Focused window #5."
    assert len(screens.shots) == 1


@pytest.mark.parametrize(
    "windows",
    [
        [],  # the window is gone from the list
        [_window(5, "com.apple.Notes", 9000, 9000)],  # on no connected display
    ],
)
async def test_focus_window_whose_display_is_unknown_shows_the_display_last_looked_at(monkeypatch, windows):
    screens = _Monitors()
    screens.windows = windows
    tools, spec = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {"display_id": 2})
    res = await tools["focus_window"].execute("c2", {"window_id": 5, "settle_ms": 0})
    assert screens.shots == [2, 2]
    assert _text(res) == (
        "Focused window #5. The screenshot is of display 2 (1280x720); your next x,y refer to it."
    )


async def test_focus_window_with_no_display_layout_shows_the_display_last_looked_at(monkeypatch):
    screens = _Monitors()
    screens.windows = [_window(5, "com.apple.Notes", 100, 100)]
    tools, _ = _session(monkeypatch, screens)
    monkeypatch.setattr(ct, "display_geometry", lambda: {})
    await tools["screenshot"].execute("c1", {"display_id": 2})
    await tools["focus_window"].execute("c2", {"window_id": 5, "settle_ms": 0})
    assert screens.shots == [2, 2]


@pytest.mark.parametrize("raised", [True, None])
async def test_focus_window_that_raised_the_window_says_focused(monkeypatch, raised):
    # None is a native module built before focus_window said whether it raised
    # the window: the result reads as it always did.
    screens = _Monitors(only_primary=True)
    screens.windows = [_window(5, "com.apple.Notes", 100, 100)]
    screens.raised = raised
    tools, _ = _session(monkeypatch, screens)
    res = await tools["focus_window"].execute("c1", {"window_id": 5, "settle_ms": 0})
    assert not res.is_error
    assert _text(res) == "Focused window #5."


async def test_focus_window_that_brought_only_the_app_forward_says_so(monkeypatch):
    # The app has several windows that could not be told apart, so it was
    # activated but window 5 was not raised. Another of its windows may be the
    # one in front, and the model must not read the screenshot as window 5.
    screens = _Monitors(only_primary=True)
    screens.windows = [_window(5, "com.apple.Notes", 100, 100)]
    screens.raised = False
    tools, _ = _session(monkeypatch, screens)
    res = await tools["focus_window"].execute("c1", {"window_id": 5, "settle_ms": 0})
    assert not res.is_error
    assert screens.sent == [("focus_window", 5)]
    assert _text(res) == (
        "Brought forward the app of window #5, but could not raise window #5 itself. "
        "Another window of that app may be in front of it."
    )


async def test_focus_window_that_brought_only_the_app_forward_names_the_display(monkeypatch):
    screens = _Monitors()
    screens.windows = [_window(5, "com.apple.Notes", -2000, 200)]
    screens.raised = False
    tools, _ = _session(monkeypatch, screens)
    await tools["screenshot"].execute("c1", {})
    res = await tools["focus_window"].execute("c2", {"window_id": 5, "settle_ms": 0})
    assert screens.shots == [None, 2]
    assert _text(res) == (
        "Brought forward the app of window #5, but could not raise window #5 itself, "
        "which is on display 2. Another window of that app may be in front of it. "
        "The screenshot is of display 2 (1280x720); your next x,y refer to it."
    )


WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "native" / "freyja_native" / "python" / "freyja_native" / "__init__.py"
)


def _wrapper_over(monkeypatch, stub):
    """The freyja_native wrapper from this checkout over `stub` as its
    _native, under its own name, so no built extension is loaded."""
    name = "freyja_native_focus_test"
    monkeypatch.setitem(sys.modules, f"{name}._native", stub)
    spec = importlib.util.spec_from_file_location(name, WRAPPER, submodule_search_locations=[])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("answer", "said"), [(True, True), (False, False), (None, None)])
def test_the_wrapper_passes_on_whether_focus_window_raised_the_window(monkeypatch, answer, said):
    # None is what a native module built before the answer existed returns.
    asked = []

    def focus_window(window_id):
        asked.append(window_id)
        return answer

    native = _wrapper_over(monkeypatch, SimpleNamespace(focus_window=focus_window))
    assert native.focus_window(5) is said
    assert asked == [5]
