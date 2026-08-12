"""Live computer verbs (bridge/voice/adapters/computer.py) — visual rework.

The realtime voice model SEES images, so these verbs return a
grid-overlaid api_dims screenshot the model clicks against by pixel
(contract §12.2). Nothing here touches the real screen: the atomic
computer tools are replaced wholesale via the ``computer._ensure_tools``
seam, System Events resolution via ``computer._resolve_app``,
osascript/exec via the ``mac`` module (same style as the other adapter
suites). The fake screenshot is a REAL PNG so the PIL grid overlay + the
dimension read exercise the true code path.
"""

from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from bridge.voice.adapters import computer, mac
from bridge.voice.verbs import VerbRegistry

# ── fakes ─────────────────────────────────────────────────────────────────


def _png_bytes(w=1280, h=800):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (20, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


class FakeTool:
    """Duck-types an atomic computer tool: async execute(call_id, args)."""

    def __init__(self, name, result=None):
        self.name = name
        self.calls = []
        self.result = result if result is not None else _ok_result(f"{name} ok")

    async def execute(self, call_id, arguments):
        self.calls.append(dict(arguments))
        return self.result


def _ok_result(content):
    return SimpleNamespace(is_error=False, content=content)


def _err_result(content):
    return SimpleNamespace(is_error=True, content=content)


def _image_result(data=None, mime="image/png"):
    data = data if data is not None else _png_bytes()
    return SimpleNamespace(
        is_error=False,
        content=[
            SimpleNamespace(text="Captured screenshot 1280x800"),
            SimpleNamespace(data=base64.b64encode(data).decode("ascii"), media_type=mime),
        ],
    )


TREE = {
    "role": "AXApplication",
    "title": "Mail",
    "children": [
        {
            "role": "AXWindow",
            "title": "Inbox",
            "children": [
                {"role": "AXButton", "title": "Compose", "bounds": [100, 40, 60, 24]},
                {"role": "AXTextField", "description": "Search", "bounds": [300, 40, 200, 24]},
                {"role": "AXLink", "title": "L" * 80, "bounds": [10, 100, 50, 12]},
                {
                    "role": "AXGroup",
                    "children": [
                        {"role": "AXCheckBox", "title": "Unread only", "bounds": [12, 200, 20, 20]}
                    ],
                },
                {"role": "AXStaticText", "title": "not interactive", "bounds": [0, 0, 10, 10]},
                {"role": "AXButton", "title": "NoBounds"},
            ],
        },
        {
            "role": "AXWindow",
            "title": "Other",
            "children": [
                {"role": "AXButton", "title": "OtherWinButton", "bounds": [1, 1, 10, 10]}
            ],
        },
    ],
}


def _ax_result(tree):
    return _ok_result(
        "AX tree for pid=42 (bounds in API coordinate space, matching your "
        "last screenshot):\n" + json.dumps(tree)
    )


TOOL_NAMES = (
    "screenshot",
    "click",
    "type_text",
    "press_key",
    "scroll",
    "read_ax_tree",
    "find_element",
)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """Enabled registry + fake tool layer + fake System Events resolve."""
    monkeypatch.setattr(computer, "_TOOLS", None)
    monkeypatch.setattr(computer, "_SPEC", SimpleNamespace(api_dims=(1280, 800)))
    monkeypatch.setattr(computer, "_snapshot", None)
    monkeypatch.setattr(computer, "_generation", 0)
    monkeypatch.setattr(computer, "_ref_seq", 0)
    monkeypatch.setattr(computer, "_FRAMES_DIR", tmp_path / "frames")

    tools = {name: FakeTool(name) for name in TOOL_NAMES}
    tools["screenshot"].result = _image_result()
    tools["read_ax_tree"].result = _ax_result(TREE)
    monkeypatch.setattr(computer, "_ensure_tools", lambda: tools)

    async def fake_resolve(app_arg):
        return (app_arg or "Mail"), 42, "Inbox", ""

    monkeypatch.setattr(computer, "_resolve_app", fake_resolve)

    registry = VerbRegistry()
    computer.register(registry, enabled_fn=lambda: True)
    return SimpleNamespace(registry=registry, tools=tools, frames=tmp_path / "frames")


async def _run(rig, verb, args=None):
    return await rig.registry.get(verb).run(dict(args or {}))


def _assert_grid_frame(res, w=1280, h=800):
    """Every computer.* action returns a grid-overlaid api_dims PNG."""
    assert res.image_b64, "no image attached"
    assert res.image_w == w and res.image_h == h
    raw = base64.b64decode(res.image_b64)
    with Image.open(io.BytesIO(raw)) as im:
        assert (im.width, im.height) == (w, h)


# ── gating ────────────────────────────────────────────────────────────────

_GATE_ARGS = {
    "computer.see": {},
    "computer.click": {"x": 10, "y": 10},
    "computer.type": {"text": "hello"},
    "computer.press": {"key": "enter"},
    "computer.scroll": {"direction": "down"},
    "computer.menu": {"menu_path": ["File", "New Tab"]},
    "computer.open_url": {"url": "https://example.com"},
}


async def test_every_verb_gates_when_disabled(rig, monkeypatch):
    registry = VerbRegistry()
    computer.register(registry, enabled_fn=lambda: False)
    assert sorted(_GATE_ARGS) == sorted(v.name for v in registry.all())
    for name, args in _GATE_ARGS.items():
        res = await registry.get(name).run(dict(args))
        assert res.ok is False, name
        assert res.data == {"setup": "computer"}, name
        assert res.summary == "computer control is disabled — enable it in settings"
        assert "Settings → Computer Control" in res.error
        # No screenshot leaks out of a gated (never-run) verb.
        assert res.image_b64 is None, name
    # The gate fires before any tool is constructed or called.
    assert all(tool.calls == [] for tool in rig.tools.values())


async def test_gate_reads_the_signal_live(rig):
    """A settings flip applies to an already-registered verb."""
    enabled = {"on": False}
    registry = VerbRegistry()
    computer.register(registry, enabled_fn=lambda: enabled["on"])
    res = await registry.get("computer.press").run({"key": "enter"})
    assert res.ok is False and res.data == {"setup": "computer"}
    enabled["on"] = True
    res = await registry.get("computer.press").run({"key": "enter"})
    assert res.ok is True


# ── computer.see (screenshot-only, grid overlay) ───────────────────────────


async def test_see_returns_grid_screenshot(rig):
    res = await _run(rig, "computer.see")
    assert res.ok
    assert res.summary == "saw Mail: 1280x800"
    assert res.data["app"] == "Mail"
    assert res.data["window"] == "Inbox"
    assert res.data["screen"] == "1280x800"
    # No AX element listing reaches the model — it sees the pixels now.
    assert "elements" not in res.data
    assert "caption" not in res.data
    _assert_grid_frame(res)
    # AX was still queried (to back the ref fallback), for the resolved pid.
    assert rig.tools["read_ax_tree"].calls == [{"pid": 42}]
    # …and the snapshot holds the resolved refs server-side (never sent).
    snap = computer._snapshot
    assert snap.app == "Mail" and snap.pid == 42 and snap.generation == 1
    assert snap.elements["e1"] == (130, 52, "Compose")
    assert snap.elements["e2"] == (400, 52, "Search")


async def test_see_grid_is_overlaid_not_bare(rig):
    """The returned PNG differs from a bare capture — the grid was drawn."""
    bare = _png_bytes()
    res = await _run(rig, "computer.see")
    assert base64.b64decode(res.image_b64) != bare


async def test_see_records_screenshot_and_prunes(rig):
    frames = rig.frames
    frames.mkdir(parents=True)
    for i in range(12):
        (frames / f"see-{i:04d}.png").write_bytes(b"old")
    res = await _run(rig, "computer.see")
    path = res.data["screenshotPath"]
    assert path.startswith(str(frames))
    remaining = sorted(p.name for p in frames.iterdir())
    assert len(remaining) == 10  # 12 old + 1 new, pruned to the newest 10
    assert path.endswith(remaining[-1])
    assert "see-0000.png" not in remaining


async def test_see_ax_opaque_app_still_returns_frame(rig):
    """An app with no usable AX tree: no refs, but the model still gets a
    screenshot to click by pixel."""
    rig.tools["read_ax_tree"].result = _err_result("read_ax_tree failed: no tree")
    res = await _run(rig, "computer.see")
    assert res.ok
    _assert_grid_frame(res)
    assert computer._snapshot.elements == {}


async def test_see_setup_failure_surfaces_when_blind(rig):
    """AX AND screenshot both blocked by a setup problem → surface it."""
    rig.tools["read_ax_tree"].result = _err_result(
        "Error: freyja_native not available (no module)"
    )
    rig.tools["screenshot"].result = _err_result(
        "screenshot: Screen Recording permission is missing"
    )
    res = await _run(rig, "computer.see")
    assert res.ok is False
    assert res.data == {"setup": "computer"}


async def test_see_app_resolution_failure(rig, monkeypatch):
    async def fail_resolve(app_arg):
        return app_arg, None, "", "Can't get application process \"Nope\""

    monkeypatch.setattr(computer, "_resolve_app", fail_resolve)
    res = await _run(rig, "computer.see", {"app": "Nope"})
    assert res.ok is False
    assert "couldn't see Nope" in res.summary
    assert res.image_b64 is None


# ── computer.click (pixel primary + fallbacks; result screenshot) ──────────


async def test_click_by_pixel_is_primary(rig):
    res = await _run(rig, "computer.click", {"x": 640, "y": 200})
    assert res.ok
    assert res.summary == "clicked (640, 200)"
    (call,) = rig.tools["click"].calls
    assert (call["x"], call["y"]) == (640, 200)
    # …and the model gets a fresh look at what the click did.
    _assert_grid_frame(res)


async def test_click_pixel_zero_zero_is_valid(rig):
    res = await _run(rig, "computer.click", {"x": 0, "y": 0})
    assert res.ok
    (call,) = rig.tools["click"].calls
    assert (call["x"], call["y"]) == (0, 0)


async def test_click_by_fresh_ref_uses_cached_coordinates(rig):
    await _run(rig, "computer.see")
    res = await _run(rig, "computer.click", {"ref": "e1"})
    assert res.ok
    assert res.summary == "clicked Compose"
    (call,) = rig.tools["click"].calls
    assert (call["x"], call["y"]) == (130, 52)
    _assert_grid_frame(res)


async def test_click_stale_generation_ref_refused(rig):
    await _run(rig, "computer.see")  # e1..e4
    await _run(rig, "computer.see")  # e5..e8 supersede
    res = await _run(rig, "computer.click", {"ref": "e1"})
    assert res.ok is False
    assert "run computer.see first" in res.summary
    assert rig.tools["click"].calls == []
    assert res.image_b64 is None


async def test_click_ref_without_any_see_refused(rig):
    res = await _run(rig, "computer.click", {"ref": "e1"})
    assert res.ok is False
    assert "run computer.see first" in res.summary
    assert rig.tools["click"].calls == []


async def test_click_by_element_label_queries_find_element(rig):
    rig.tools["find_element"].result = _ok_result(
        "Found element: bounds=(10, 20, 100x40), center=(60, 40). "
        "Coordinates are in the SAME space as your last screenshot."
    )
    res = await _run(rig, "computer.click", {"element": "Compose"})
    assert res.ok
    assert res.summary == "clicked Compose"
    assert rig.tools["find_element"].calls == [{"pid": 42, "label": "Compose"}]
    (call,) = rig.tools["click"].calls
    assert (call["x"], call["y"]) == (60, 40)
    _assert_grid_frame(res)


async def test_click_by_element_not_found(rig):
    rig.tools["find_element"].result = _ok_result(
        "No element found for pid=42 label='Zilch'. Try relaxing the query."
    )
    res = await _run(rig, "computer.click", {"element": "Zilch"})
    assert res.ok is False
    assert "Zilch" in res.summary
    assert rig.tools["click"].calls == []


async def test_click_without_target_refused(rig):
    res = await _run(rig, "computer.click")
    assert res.ok is False
    assert rig.tools["click"].calls == []
    assert res.image_b64 is None


async def test_click_permission_error_maps_to_setup(rig):
    rig.tools["click"].result = _err_result(
        "click: Accessibility permission is NOT working. macOS is silently "
        "dropping CGEvent injection.\n\nFIX:\n  1. Open System Settings"
    )
    res = await _run(rig, "computer.click", {"x": 5, "y": 6})
    assert res.ok is False
    assert res.data == {"setup": "computer"}
    assert "Accessibility" in res.error
    # A failed action returns no screenshot — nothing changed to look at.
    assert res.image_b64 is None


# ── computer.type ─────────────────────────────────────────────────────────


async def test_type_passes_full_text_and_returns_frame(rig):
    res = await _run(rig, "computer.type", {"text": "hello there"})
    assert res.ok
    assert res.summary == 'typed "hello there"'
    assert rig.tools["type_text"].calls == [{"text": "hello there"}]
    _assert_grid_frame(res)


async def test_type_summary_truncates_long_text(rig):
    text = "x" * 60
    res = await _run(rig, "computer.type", {"text": text})
    assert res.ok
    # The tool (and therefore the receipt args) got the full text…
    assert rig.tools["type_text"].calls == [{"text": text}]
    # …but the spoken/journaled summary never carries it verbatim.
    assert text not in res.summary
    assert res.summary == f'typed "{"x" * 39}…"'


async def test_type_requires_text(rig):
    res = await _run(rig, "computer.type", {"text": ""})
    assert res.ok is False
    assert rig.tools["type_text"].calls == []
    assert res.image_b64 is None


# ── computer.press ────────────────────────────────────────────────────────


async def test_press_plain_key(rig):
    res = await _run(rig, "computer.press", {"key": "enter"})
    assert res.ok and res.summary == "pressed enter"
    assert rig.tools["press_key"].calls == [{"key": "enter", "modifiers": []}]
    _assert_grid_frame(res)


async def test_press_with_modifiers(rig):
    res = await _run(rig, "computer.press", {"key": "t", "modifiers": ["cmd"]})
    assert res.ok and res.summary == "pressed cmd+t"
    assert rig.tools["press_key"].calls == [{"key": "t", "modifiers": ["cmd"]}]


async def test_press_combo_string_maps_to_modifiers(rig):
    res = await _run(rig, "computer.press", {"key": "cmd+shift+t"})
    assert res.ok and res.summary == "pressed cmd+shift+t"
    assert rig.tools["press_key"].calls == [{"key": "t", "modifiers": ["cmd", "shift"]}]


async def test_press_normalizes_modifier_aliases(rig):
    await _run(rig, "computer.press", {"key": "a", "modifiers": ["Command", "Option"]})
    assert rig.tools["press_key"].calls == [{"key": "a", "modifiers": ["cmd", "alt"]}]


# ── computer.scroll ───────────────────────────────────────────────────────


async def test_scroll_down_default_amount(rig):
    res = await _run(rig, "computer.scroll", {"direction": "down"})
    assert res.ok and res.summary == "scrolled down"
    assert rig.tools["scroll"].calls == [{"dx": 0, "dy": 8}]
    _assert_grid_frame(res)


async def test_scroll_directions_and_amount(rig):
    await _run(rig, "computer.scroll", {"direction": "up", "amount": 3})
    await _run(rig, "computer.scroll", {"direction": "left"})
    await _run(rig, "computer.scroll", {"direction": "right", "amount": 2})
    assert rig.tools["scroll"].calls == [
        {"dx": 0, "dy": -3},
        {"dx": -8, "dy": 0},
        {"dx": 2, "dy": 0},
    ]


async def test_scroll_at_ref_uses_cached_point(rig):
    await _run(rig, "computer.see")
    res = await _run(rig, "computer.scroll", {"direction": "down", "ref": "e2"})
    assert res.ok
    assert rig.tools["scroll"].calls == [{"dx": 0, "dy": 8, "x": 400, "y": 52}]


async def test_scroll_at_pixel(rig):
    res = await _run(rig, "computer.scroll", {"direction": "up", "x": 100, "y": 200})
    assert res.ok
    assert rig.tools["scroll"].calls == [{"dx": 0, "dy": -8, "x": 100, "y": 200}]


async def test_scroll_stale_ref_refused(rig):
    await _run(rig, "computer.see")
    await _run(rig, "computer.see")
    res = await _run(rig, "computer.scroll", {"direction": "down", "ref": "e1"})
    assert res.ok is False
    assert "run computer.see first" in res.summary
    assert rig.tools["scroll"].calls == []
    assert res.image_b64 is None


async def test_scroll_bad_direction_refused(rig):
    res = await _run(rig, "computer.scroll", {"direction": "sideways"})
    assert res.ok is False
    assert rig.tools["scroll"].calls == []


# ── computer.menu ─────────────────────────────────────────────────────────


class OsaRecorder:
    def __init__(self, ok=True, out=""):
        self.scripts = []
        self.ok = ok
        self.out = out

    async def __call__(self, script, timeout=6.0):
        self.scripts.append(script)
        return self.ok, self.out


async def test_menu_frontmost_two_level_path(rig, monkeypatch):
    osa = OsaRecorder()
    monkeypatch.setattr(mac, "run_osascript", osa)
    res = await _run(rig, "computer.menu", {"menu_path": ["File", "New Tab"]})
    assert res.ok
    assert res.summary == "menu: File → New Tab"
    (script,) = osa.scripts
    assert script == (
        'tell application "System Events"\n'
        "tell (first application process whose frontmost is true)\n"
        "set frontmost to true\n"
        'click menu item "New Tab" of menu "File" of menu bar 1\n'
        "end tell\n"
        "end tell"
    )
    # menu returns a screenshot after (§12.2).
    _assert_grid_frame(res)


async def test_menu_named_app_nested_path_quotes_every_segment(rig, monkeypatch):
    osa = OsaRecorder()
    monkeypatch.setattr(mac, "run_osascript", osa)
    res = await _run(
        rig,
        "computer.menu",
        {"menu_path": ["Format", 'Fo"nt', "Bold"], "app": "TextEdit"},
    )
    assert res.ok
    (script,) = osa.scripts
    assert script == (
        'tell application "System Events"\n'
        'tell process "TextEdit"\n'
        "set frontmost to true\n"
        'click menu item "Bold" of menu "Fo\\"nt" of menu item "Fo\\"nt" '
        'of menu "Format" of menu bar 1\n'
        "end tell\n"
        "end tell"
    )


async def test_menu_needs_at_least_menu_and_item(rig, monkeypatch):
    osa = OsaRecorder()
    monkeypatch.setattr(mac, "run_osascript", osa)
    res = await _run(rig, "computer.menu", {"menu_path": ["File"]})
    assert res.ok is False
    assert osa.scripts == []
    assert res.image_b64 is None


async def test_menu_assistive_access_denial_maps_to_setup(rig, monkeypatch):
    osa = OsaRecorder(ok=False, out="osascript is not allowed assistive access")
    monkeypatch.setattr(mac, "run_osascript", osa)
    res = await _run(rig, "computer.menu", {"menu_path": ["File", "New Tab"]})
    assert res.ok is False
    assert res.data == {"setup": "computer"}


# ── computer.open_url (no screenshot — it leaves this app) ────────────────


class ExecRecorder:
    def __init__(self, ok=True, out=""):
        self.calls = []
        self.ok = ok
        self.out = out

    async def __call__(self, argv, timeout=6.0):
        self.calls.append(list(argv))
        return self.ok, self.out


async def test_open_url_https(rig, monkeypatch):
    execer = ExecRecorder()
    monkeypatch.setattr(mac, "run_exec", execer)
    res = await _run(rig, "computer.open_url", {"url": "https://example.com/x?y=1"})
    assert res.ok
    assert res.summary == "opened example.com"
    assert execer.calls == [["open", "https://example.com/x?y=1"]]


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "file:///etc/passwd", "notaurl", "ftp://x.com", "http://"],
)
async def test_open_url_rejects_non_http_schemes(rig, monkeypatch, url):
    execer = ExecRecorder()
    monkeypatch.setattr(mac, "run_exec", execer)
    res = await _run(rig, "computer.open_url", {"url": url})
    assert res.ok is False
    assert execer.calls == []


# ── the grid overlay helper ──────────────────────────────────────────────


def test_grid_overlay_is_valid_png_of_same_size():
    src = _png_bytes(300, 200)
    out = computer._grid_overlay(src, 300, 200)
    with Image.open(io.BytesIO(out)) as im:
        assert (im.width, im.height) == (300, 200)
    assert out != src  # lines/labels were drawn


def test_grid_overlay_on_garbage_returns_input():
    """A non-image byte blob must degrade to itself, never raise."""
    garbage = b"not a png"
    assert computer._grid_overlay(garbage, 10, 10) == garbage


# ── receipts + image passthrough flow through the service ─────────────────


async def test_receipts_and_image_flow_through_service_execute(rig, tmp_path):
    from bridge.voice.service import VoiceService

    events = []
    svc = VoiceService(
        SimpleNamespace(default_model="test-model", computer_enabled=True),
        base_dir=tmp_path / "voice",
        registry=rig.registry,
        emit_fn=events.append,
    )
    await svc.handle_tool_call(
        {
            "voiceSessionId": "v1",
            "callId": "c1",
            "name": "act",
            "argumentsJson": json.dumps({"verb": "computer.press", "args": {"key": "enter"}}),
        }
    )
    (result,) = [e for e in events if e["type"] == "voice_tool_result"]
    assert result["ok"] is True
    assert json.loads(result["output"])["summary"] == "pressed enter"
    # The screenshot rode through onto the event (contract §12.1).
    assert result["imageB64"]
    assert result["imageW"] == 1280 and result["imageH"] == 800
    receipt = result["receipt"]
    assert receipt["verb"] == "computer.press"
    assert receipt["lane"] == "brain"
    assert receipt["ok"] is True
    assert receipt["args"] == {"key": "enter"}
    (live_receipt,) = [e for e in events if e["type"] == "voice_receipt"]
    assert live_receipt["receipt"]["id"] == receipt["id"]
    # Persisted too — the receipts file is the audit trail.
    lines = (tmp_path / "voice" / "receipts.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[-1])["verb"] == "computer.press"


# ── panic interrupts an in-flight action ─────────────────────────────────


async def test_cancel_inflight_trips_shared_cancel_event(rig, monkeypatch):
    """On panic the service calls cancel_inflight(); it must set the spec's
    cancel_event so a verb in flight aborts at its next checkpoint."""
    import asyncio as _asyncio

    fake_spec = SimpleNamespace(cancel_event=_asyncio.Event())
    monkeypatch.setattr(computer, "_SPEC", fake_spec)
    assert not fake_spec.cancel_event.is_set()
    computer.cancel_inflight()
    assert fake_spec.cancel_event.is_set()


async def test_run_tool_clears_stale_cancel_before_next_verb(rig, monkeypatch):
    """A fresh verb after a prior panic must start unblocked — _run_tool
    clears the cancel event before executing."""
    import asyncio as _asyncio

    fake_spec = SimpleNamespace(cancel_event=_asyncio.Event(), api_dims=(1280, 800))
    fake_spec.cancel_event.set()  # stale trip from a previous panic
    monkeypatch.setattr(computer, "_SPEC", fake_spec)
    res = await _run(rig, "computer.press", {"key": "enter"})
    assert res.ok
    assert not fake_spec.cancel_event.is_set()  # cleared before the run


async def test_cancel_inflight_no_spec_is_safe(monkeypatch):
    monkeypatch.setattr(computer, "_SPEC", None)
    computer.cancel_inflight()  # must not raise


# ── vision-grounded click (the describe-what-you-see fallback) ────────────


async def test_click_by_target_uses_vision_grounding(rig, monkeypatch):
    """No pixel and no ref — click by describing what you see. Vision
    returns normalized coords; we scale by api_dims and click."""
    calls = []

    async def fake_locate(jpeg, target):
        calls.append((jpeg, target))
        return (0.5, 0.25)  # center-x, quarter-down

    monkeypatch.setattr(computer.screen, "locate_in_image", fake_locate)

    res = await _run(rig, "computer.click", {"target": "the Hacker News tab"})
    assert res.ok, res.summary
    assert res.summary == "clicked the Hacker News tab"
    (call,) = rig.tools["click"].calls
    assert (call["x"], call["y"]) == (640, 200)  # 0.5*1280, 0.25*800
    assert calls and calls[0][1] == "the Hacker News tab"
    _assert_grid_frame(res)


async def test_click_by_target_not_found_asks(rig, monkeypatch):
    async def fake_locate(jpeg, target):
        return None  # vision couldn't see it

    monkeypatch.setattr(computer.screen, "locate_in_image", fake_locate)
    res = await _run(rig, "computer.click", {"target": "a unicorn"})
    assert res.ok is False
    assert "unicorn" in res.summary
    assert rig.tools["click"].calls == []
